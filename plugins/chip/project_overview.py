"""Generate the chip plugin's run-level OVERVIEW.md after Stage 6 artifacts.

This module deliberately owns the Overview lifecycle inside the chip plugin. It
uses Stage 6's existing output hook rather than adding a public Pipeline stage
or treating the Overview as another per-module Profile artifact.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from config import OPENCODE_MAX_RETRIES, OPENCODE_SPEC_MODEL
from src.generate_batch_prompts import (
    build_expected_dependencies_by_file,
    expected_dependencies_for_file,
)
from src.file_utils import _is_test_file
from src.languages.hardware import (
    CHISEL_EXTENSIONS,
    VERILOG_EXTENSIONS,
    is_excluded_source_directory,
    resolve_hardware_project_paths,
)
from src.llm_client import build_llm_cli_command
from src.opencode_trace import run_opencode_traced

from .detection import read_plugin_submodules
from .profiles import PROFILES


_TOPDOWN_FILENAME_RE = re.compile(r"^phase_(?P<phase>\d+)_topdown_layers\.json$")
_OVERVIEW_INDEX_MARKER = "<!-- FM_AGENT_SPEC_INDEX -->"
_TEMPLATE_PLACEHOLDERS = (
    "<analysis-derived DUT or analysis-scope name>",
    "<short introduction>",
    "<DUT or Analysis Scope Name>",
    "<Brief description of the verification target and analyzed scope.>",
)


@dataclass(frozen=True)
class OverviewUnit:
    """A top-down hardware unit selected for the Overview spec index."""

    name: str
    source_path: Path
    source_relpath: str
    artifact_eligible: bool
    is_module: bool | None
    all_callees: tuple[str, ...]

    @property
    def module_name(self) -> str:
        return self.name.rsplit("::", 1)[-1]


@dataclass(frozen=True)
class OverviewInputs:
    """Verified, run-scoped material supplied to the Overview writer."""

    project_root: Path
    work_dir: Path
    dialect: str
    eligible_units: tuple[OverviewUnit, ...]
    root_candidates: tuple[OverviewUnit, ...]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to read JSON input {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


def _write_text_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    _write_text_atomically(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def _work_paths(proj_dir: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    project_root, work_dir = resolve_hardware_project_paths(proj_dir)
    overview_dir = work_dir / "chip" / "overview"
    return project_root, work_dir, overview_dir


def prepare_project_overview(proj_dir: str) -> None:
    """Move a prior generated Overview out of this run's published location.

    This is intentionally independent of the Chisel eligibility hook, because
    Verilog runs also need to invalidate an old run-level Overview before Stage 6.
    """
    _, work_dir, overview_dir = _work_paths(proj_dir)
    final_overview = work_dir / "OVERVIEW.md"
    previous_overview = overview_dir / "OVERVIEW.previous.md"
    overview_dir.mkdir(parents=True, exist_ok=True)
    if final_overview.exists():
        if not final_overview.is_file():
            raise RuntimeError(f"cannot prepare chip Overview: expected a file at {final_overview}")
        os.replace(final_overview, previous_overview)
        logging.info("Chip Overview: moved prior output to %s", previous_overview)


def _phase_numbers(phases_data: dict[str, Any]) -> tuple[int, ...]:
    phases = phases_data.get("phases")
    if not isinstance(phases, list):
        raise ValueError("phases.json must contain a 'phases' array")

    numbers: list[int] = []
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("phases.json phase entries must be objects")
        number = phase.get("phase")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("each phases.json phase must have a positive integer 'phase'")
        numbers.append(number)
    if len(set(numbers)) != len(numbers):
        raise ValueError("phases.json contains duplicate phase numbers")
    return tuple(sorted(numbers))


def _dialect_from_phases(phases_data: dict[str, Any]) -> str:
    raw_languages = phases_data.get("languages")
    if not isinstance(raw_languages, list | tuple | set):
        raise ValueError("phases.json must record the selected chip language")
    languages = {
        value.strip().lower()
        for value in raw_languages
        if isinstance(value, str) and value.strip()
    }
    if len(languages) != 1 or not languages <= set(PROFILES):
        raise ValueError(
            "Overview generation requires exactly one supported chip language in "
            f"phases.json; found {sorted(languages)!r}"
        )
    return next(iter(languages))


def _resolve_work_relative_path(work_dir: Path, raw_path: object) -> tuple[Path, str]:
    """Resolve an extracted-unit path and prevent it from escaping fm_agent."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("topdown function entry is missing a non-empty 'file'")
    value = raw_path.replace("\\", "/")
    prefix = f"{work_dir.name}/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = work_dir / candidate
    candidate = Path(os.path.realpath(candidate))
    work_root = Path(os.path.realpath(work_dir))
    try:
        relpath = candidate.relative_to(work_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"topdown extracted file escapes fm_agent: {raw_path!r}") from exc
    return candidate, relpath


def _topdown_paths_for_phases(work_dir: Path, phase_numbers: Iterable[int]) -> tuple[Path, ...]:
    topdown_paths: list[Path] = []
    for phase in phase_numbers:
        path = work_dir / "spec_prompts" / f"phase_{phase:02d}_topdown_layers.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"missing current phase topdown graph for Overview generation: {path}"
            )
        if not _TOPDOWN_FILENAME_RE.fullmatch(path.name):
            raise ValueError(f"invalid topdown graph filename: {path}")
        topdown_paths.append(path)
    return tuple(topdown_paths)


def _read_units(
    topdown_paths: Iterable[Path],
    work_dir: Path,
) -> tuple[tuple[OverviewUnit, ...], dict[str, dict[str, Any]]]:
    """Merge current phase graphs while preserving context-only nodes."""
    by_name: dict[str, OverviewUnit] = {}
    layers_by_path: dict[str, dict[str, Any]] = {}
    for topdown_path in topdown_paths:
        data = _read_json_object(topdown_path)
        layers = data.get("layers")
        if not isinstance(layers, list):
            raise ValueError(f"topdown graph must contain a 'layers' array: {topdown_path}")
        layers_by_path[str(topdown_path)] = data
        for layer in layers:
            if not isinstance(layer, dict):
                raise ValueError(f"topdown layer must be an object: {topdown_path}")
            functions = layer.get("functions")
            if not isinstance(functions, list):
                raise ValueError(f"topdown layer must contain a 'functions' array: {topdown_path}")
            for function in functions:
                if not isinstance(function, dict):
                    raise ValueError(f"topdown function must be an object: {topdown_path}")
                name = function.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"topdown function is missing a name: {topdown_path}")
                source_path, source_relpath = _resolve_work_relative_path(
                    work_dir, function.get("file")
                )
                if not source_path.is_file():
                    raise FileNotFoundError(
                        f"topdown function points to missing extracted source: {source_path}"
                    )
                raw_callees = function.get("all_callees", ())
                if not isinstance(raw_callees, (list, tuple, set)):
                    raise ValueError(
                        f"topdown function has invalid all_callees: {name}"
                    )
                callees = tuple(sorted({
                    callee for callee in raw_callees
                    if isinstance(callee, str) and callee
                }))
                raw_is_module = function.get("is_module")
                if raw_is_module is not None and not isinstance(raw_is_module, bool):
                    raise ValueError(f"topdown function has invalid is_module metadata: {name}")
                unit = OverviewUnit(
                    name=name,
                    source_path=source_path,
                    source_relpath=source_relpath,
                    artifact_eligible=function.get("artifact_eligible", True) is not False,
                    is_module=raw_is_module,
                    all_callees=callees,
                )
                existing = by_name.get(name)
                if existing is not None and (
                    existing.source_path != unit.source_path
                    or existing.artifact_eligible != unit.artifact_eligible
                    or existing.is_module != unit.is_module
                ):
                    raise ValueError(
                        "conflicting current topdown entries for module "
                        f"{name!r}: {existing.source_relpath} vs {source_relpath}"
                    )
                if existing is not None:
                    unit = OverviewUnit(
                        name=unit.name,
                        source_path=unit.source_path,
                        source_relpath=unit.source_relpath,
                        artifact_eligible=unit.artifact_eligible,
                        is_module=unit.is_module,
                        all_callees=tuple(sorted(
                            set(existing.all_callees) | set(unit.all_callees)
                        )),
                    )
                by_name[name] = unit
    return tuple(sorted(by_name.values(), key=lambda unit: unit.name)), layers_by_path


def _root_candidates(units: Iterable[OverviewUnit]) -> tuple[OverviewUnit, ...]:
    """Return in-scope module roots while preserving module context nodes.

    Chisel's eligibility annotation distinguishes a non-Module declaration
    (Bundle/trait/type context) from a hardware Module that simply lacks a
    standalone artifact. The former must not become a DUT root, while the
    latter remains relevant to hierarchy reasoning.
    """
    by_name = {
        unit.name: unit for unit in units
        if unit.is_module is not False
    }
    called_in_scope = {
        callee
        for unit in by_name.values()
        for callee in unit.all_callees
        if callee in by_name
    }
    return tuple(
        sorted(
            (unit for name, unit in by_name.items() if name not in called_in_scope),
            key=lambda unit: unit.name,
        )
    )


def _validate_chisel_eligibility(
    eligibility_path: Path,
    eligible_units: Iterable[OverviewUnit],
) -> None:
    data = _read_json_object(eligibility_path)
    kept = data.get("kept")
    if not isinstance(kept, list):
        raise ValueError("chip eligibility manifest must contain a 'kept' array")
    kept_names = {
        entry.get("name")
        for entry in kept
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    expected_names = {unit.name for unit in eligible_units}
    if kept_names != expected_names:
        raise RuntimeError(
            "current Chisel eligibility manifest disagrees with the current "
            "topdown artifact set: "
            f"manifest_only={sorted(kept_names - expected_names)!r}; "
            f"topdown_only={sorted(expected_names - kept_names)!r}"
        )


def _collect_inputs(proj_dir: str) -> tuple[OverviewInputs, dict[str, dict[str, Any]]]:
    project_root, work_dir, _ = _work_paths(proj_dir)
    phases_path = work_dir / "phases.json"
    phases_data = _read_json_object(phases_path)
    phase_numbers = _phase_numbers(phases_data)
    dialect = _dialect_from_phases(phases_data)
    topdown_paths = _topdown_paths_for_phases(work_dir, phase_numbers)
    units, topdown_data = _read_units(topdown_paths, work_dir)
    eligible_units = tuple(unit for unit in units if unit.artifact_eligible)
    if dialect == "chisel":
        eligibility_path = work_dir / "chip" / "eligibility.json"
        if not eligibility_path.is_file():
            raise FileNotFoundError(
                f"missing current Chisel eligibility manifest: {eligibility_path}"
            )
        _validate_chisel_eligibility(eligibility_path, eligible_units)

    return OverviewInputs(
        project_root=project_root,
        work_dir=work_dir,
        dialect=dialect,
        eligible_units=eligible_units,
        root_candidates=_root_candidates(units),
    ), topdown_data


def _relative_to_work(path: Path, work_dir: Path) -> str:
    try:
        return path.resolve().relative_to(work_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"Overview input escapes fm_agent: {path}") from exc


def _validate_module_artifacts(
    inputs: OverviewInputs,
    topdown_data: dict[str, dict[str, Any]],
) -> None:
    """Require Profile-ready artifact pairs before asking the Overview writer."""
    profile = PROFILES[inputs.dialect]
    expected_by_file: dict[str, tuple[str, ...]] = {}
    for data in topdown_data.values():
        partial = build_expected_dependencies_by_file(data, inputs.work_dir)
        for file_key, dependencies in partial.items():
            expected_by_file[file_key] = tuple(sorted(
                set(expected_by_file.get(file_key, ())) | set(dependencies)
            ))

    failures: list[str] = []
    for unit in inputs.eligible_units:
        expected_dependencies = expected_dependencies_for_file(unit.source_path, expected_by_file)
        validation = profile.validate(unit.source_path, expected_dependencies)
        if not validation.ready:
            details = "; ".join(validation.errors) or "artifact validation failed"
            failures.append(f"{unit.name}: {details}")
    if failures:
        raise RuntimeError(
            "cannot generate chip Overview because module artifacts are not ready:\n- "
            + "\n- ".join(failures)
        )


def _module_spec_path(inputs: OverviewInputs, unit: OverviewUnit) -> Path:
    path = PROFILES[inputs.dialect].artifact_paths(unit.source_path).self_spec
    if not path.is_file():
        raise FileNotFoundError(f"missing validated module specification: {path}")
    return path


def _display_root_candidates(inputs: OverviewInputs) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for unit in inputs.root_candidates:
        records.append({
            "name": unit.name,
            "module_name": unit.module_name,
            "source": unit.source_relpath,
            "artifact_status": "standalone_spec" if unit.artifact_eligible else "context_only",
        })
    return records


def _source_inventory(inputs: OverviewInputs) -> dict[str, Any]:
    """List raw HDL sources the Overview writer may inspect on demand.

    This deliberately records paths rather than trying to prove a Scala-to-RTL
    elaboration mapping. The writer may use the list as optional context for a
    useful reviewer-facing summary.
    """
    submodules = read_plugin_submodules(inputs.project_root)
    scan_roots = [inputs.project_root / path for path in submodules]
    if not scan_roots:
        scan_roots = [inputs.project_root]

    chisel_sources: list[str] = []
    rtl_sources: list[str] = []
    project_root = inputs.project_root.resolve()
    seen: set[str] = set()
    for scan_root in scan_roots:
        for current_root, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = sorted(
                name for name in dirnames if not is_excluded_source_directory(name)
            )
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                path = Path(current_root) / filename
                suffix = path.suffix.lower()
                if suffix not in CHISEL_EXTENSIONS | VERILOG_EXTENSIONS:
                    continue
                relative = path.resolve().relative_to(project_root).as_posix()
                if relative in seen or _is_test_file(relative):
                    continue
                seen.add(relative)
                if suffix in CHISEL_EXTENSIONS:
                    chisel_sources.append(relative)
                else:
                    rtl_sources.append(relative)

    return {
        "purpose": (
            "Raw HDL source inventory for optional Overview exploration. "
            "It is a path index, not an elaboration manifest or primary evidence."
        ),
        "selected_dialect": inputs.dialect,
        "analysis_scope": {
            "submodules": list(submodules),
            "root": "." if not submodules else None,
        },
        "chisel_sources": chisel_sources,
        "rtl_sources": rtl_sources,
        "exploration_rules": [
            "Use the supplied final module specifications as the primary evidence.",
            "Use raw HDL only to supplement a useful reader-facing summary.",
            "Explore only files useful for the requested summary; exhaustive source reconciliation is not required.",
            "If raw HDL materially conflicts with a final specification, do not present the raw detail as confirmed fact.",
        ],
    }


def _input_manifest(inputs: OverviewInputs, source_inventory_path: Path) -> dict[str, Any]:
    """Create the spec-first evidence index supplied to the Overview writer.

    Stage 1--5 artifacts remain hook-internal: they establish dialect, scope,
    artifact readiness, and the final link index, but are deliberately not
    attached to the writer's LLM context.
    """
    work_dir = inputs.work_dir
    specification_records = []
    for unit in inputs.eligible_units:
        specification_records.append({
            "name": unit.name,
            "module_name": unit.module_name,
            "specification": _relative_to_work(
                _module_spec_path(inputs, unit), work_dir
            ),
        })
    return {
        "purpose": (
            "Spec-first evidence index for the chip run-level Overview generator. "
            "Listed final module specifications are the primary evidence."
        ),
        "dialect": inputs.dialect,
        "analysis_scope": "Current hardware analysis scope represented by the validated specifications below.",
        "root_candidates": _display_root_candidates(inputs),
        "root_interpretation": (
            "A root has no known parent inside this analysis scope. It is not "
            "proof of a whole-chip top module; external parents may be absent."
        ),
        "inputs": {
            "source_inventory": _relative_to_work(source_inventory_path, work_dir),
        },
        "specifications": specification_records,
        "specification_index_rule": (
            "The generator replaces the marker with links to exactly the listed "
            "validated module specifications."
        ),
    }


def _input_file_paths(
    inputs: OverviewInputs,
    manifest_path: Path,
    source_inventory_path: Path,
) -> list[Path]:
    paths = [manifest_path, source_inventory_path]
    paths.extend(_module_spec_path(inputs, unit) for unit in inputs.eligible_units)
    return list(dict.fromkeys(paths))


def _spec_index(inputs: OverviewInputs) -> str:
    """Render the final spec index from Profile-validated module artifacts."""
    if not inputs.eligible_units:
        return "No standalone module specification documents were generated for this analysis scope."

    entries = [
        "FM-Agent generated the following standalone module specifications:",
        "",
    ]
    counts: dict[str, int] = {}
    for unit in inputs.eligible_units:
        counts[unit.module_name] = counts.get(unit.module_name, 0) + 1
    for unit in sorted(inputs.eligible_units, key=lambda item: item.name):
        label = unit.module_name
        if counts[label] > 1:
            label = f"{label} ({unit.name})"
        spec_path = _module_spec_path(inputs, unit)
        relpath = _relative_to_work(spec_path, inputs.work_dir)
        entries.append(f"- [{label} specification](<{relpath}>)")
    return "\n".join(entries)


def _validate_overview_structure(content: str, expect_index_marker: bool) -> list[str]:
    """Keep publication checks lightweight while preserving a usable artifact.

    The Overview is a human-facing summary of generated specs. Detailed content
    and section balance are intentionally left for review instead of encoded as
    a brittle acceptance gate.
    """
    errors: list[str] = []
    if not content.strip():
        return ["OVERVIEW is empty"]
    if not re.search(r"^#[ \t]+\S", content, re.MULTILINE):
        errors.append("OVERVIEW must contain a non-empty level-one title")
    for placeholder in _TEMPLATE_PLACEHOLDERS:
        if placeholder in content:
            errors.append(f"OVERVIEW contains an unreplaced template placeholder: {placeholder}")
    marker_count = content.count(_OVERVIEW_INDEX_MARKER)
    if expect_index_marker and marker_count != 1:
        errors.append("OVERVIEW must contain the specification-index marker exactly once")
    if not expect_index_marker and marker_count:
        errors.append("published OVERVIEW must not retain the specification-index marker")
    return errors


def _validate_published_links(content: str, work_dir: Path) -> list[str]:
    """Check local links without rejecting useful external reviewer links."""
    errors: list[str] = []
    for match in re.finditer(r"\[[^\]]+\]\((?:<([^>]+)>|([^\s)]+))\)", content):
        target = (match.group(1) or match.group(2) or "").split("#", 1)[0]
        if not target:
            continue
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
            continue
        path = Path(target)
        if path.is_absolute():
            continue
        resolved = Path(os.path.realpath(work_dir / path))
        try:
            resolved.relative_to(Path(os.path.realpath(work_dir)))
        except ValueError:
            errors.append(f"OVERVIEW local link escapes fm_agent: {target}")
            continue
        if not resolved.is_file():
            errors.append(f"OVERVIEW local link target does not exist: {target}")
    return errors


def _publish_candidate(pending_path: Path, final_path: Path, inputs: OverviewInputs) -> list[str]:
    try:
        candidate = pending_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"OVERVIEW candidate was not written: {exc}"]
    errors = _validate_overview_structure(candidate, expect_index_marker=True)
    if errors:
        return errors
    published = candidate.replace(_OVERVIEW_INDEX_MARKER, _spec_index(inputs))
    errors = _validate_overview_structure(published, expect_index_marker=False)
    errors.extend(_validate_published_links(published, inputs.work_dir))
    if errors:
        return errors
    _write_text_atomically(final_path, published)
    return []


def _empty_overview() -> str:
    """Return the no-artifact fallback without making an LLM call."""
    return """# Chip Analysis Scope

This Overview records the selected hardware analysis scope. No artifact-eligible hardware modules were available for standalone specification generation.

## 1. Device Under Test (DUT) Description

The analysis scope does not provide a standalone DUT artifact for this run.

### 1.1 Module Parameters

N/A — no artifact-eligible DUT module was identified.

### 1.2 Interface List

N/A — no artifact-eligible DUT module was identified.

### 1.3 Functional Description

The available analysis metadata does not establish a standalone DUT behavior for this run.

## 2. Verification Requirements

### 2.1 Verification Objectives

No standalone module specification is available to define verification objectives for this scope.

### 2.2 Verification Plan Structure

No FG/FC/CK specification tree was generated for this scope.

## 3. Additional Notes

This FM-Agent-generated document summarizes the available analysis scope. PEP 8 applies only to future Python test code when such code is written.

## 4. Bug Analysis

No test execution, verification result, or bug finding is implied by this document. Future failures should record the expected behavior, observed behavior, and supporting source or specification evidence.

## 5. Specification Documents

No standalone module specification documents were generated for this analysis scope.
"""


def _generation_prompt(manifest_relpath: str, feedback: list[str]) -> str:
    prompt = (
        "Generate the chip run-level OVERVIEW.md now. Read the workflow instruction and "
        f"the spec-first evidence index at fm_agent/{manifest_relpath}. Read every "
        "listed final module specification before writing the requested candidate file. "
        "Use its source inventory only for controlled, on-demand exploration of "
        "relevant raw HDL sources."
    )
    if feedback:
        prompt += "\n\nThe previous candidate was rejected. Correct all of these issues:\n- " + "\n- ".join(feedback)
    return prompt


def _stage_workflow_prompt(work_dir: Path) -> Path:
    """Copy the plugin workflow into this run's ``fm_agent`` workspace.

    Pipeline prompts are run artifacts: preserving this exact copy makes the
    OpenCode trace self-contained and prevents a later source-tree prompt edit
    from changing how an existing run is interpreted.
    """
    source_path = Path(__file__).with_name("prompts") / "workflow_generate_project_overview.md"
    if not source_path.is_file():
        raise FileNotFoundError(f"missing chip Overview workflow prompt: {source_path}")
    destination_path = work_dir / "workflow_generate_project_overview.md"
    if source_path.resolve() != destination_path.resolve():
        shutil.copy2(source_path, destination_path)
    return destination_path


def generate_project_overview_document(proj_dir: str) -> None:
    """Generate, validate, and atomically publish ``fm_agent/OVERVIEW.md``."""
    inputs, topdown_data = _collect_inputs(proj_dir)
    _validate_module_artifacts(inputs, topdown_data)
    _, _, overview_dir = _work_paths(proj_dir)
    final_path = inputs.work_dir / "OVERVIEW.md"
    pending_path = overview_dir / "OVERVIEW.pending.md"

    if not inputs.eligible_units:
        content = _empty_overview()
        errors = _validate_overview_structure(content, expect_index_marker=False)
        if errors:
            raise RuntimeError("internal empty OVERVIEW template is invalid: " + "; ".join(errors))
        _write_text_atomically(final_path, content)
        logging.info("Chip Overview: published no-artifact fallback to %s", final_path)
        return

    source_inventory_path = overview_dir / "OVERVIEW.source_inventory.json"
    _write_json_atomically(source_inventory_path, _source_inventory(inputs))
    manifest_path = overview_dir / "OVERVIEW.input.json"
    _write_json_atomically(manifest_path, _input_manifest(inputs, source_inventory_path))
    workflow_path = _stage_workflow_prompt(inputs.work_dir)
    input_paths = [workflow_path, *_input_file_paths(inputs, manifest_path, source_inventory_path)]
    input_relpaths = [_relative_to_work(path, inputs.work_dir) for path in input_paths]
    feedback: list[str] = []
    attempts = max(1, OPENCODE_MAX_RETRIES)
    for attempt in range(1, attempts + 1):
        pending_path.unlink(missing_ok=True)
        command = build_llm_cli_command(
            model=OPENCODE_SPEC_MODEL,
            prompt=_generation_prompt(_relative_to_work(manifest_path, inputs.work_dir), feedback),
            cwd=str(inputs.project_root),
            files=[str(path) for path in input_paths],
        )
        try:
            run_opencode_traced(
                proj_dir=str(inputs.project_root),
                work_dir=str(inputs.work_dir),
                command=command,
                stage="chip_project_overview",
                function_ids=[unit.name for unit in inputs.eligible_units],
                input_files=input_relpaths,
                output_files=["chip/overview/OVERVIEW.pending.md"],
                summary=(f"Generate chip run-level OVERVIEW.md (attempt {attempt}/{attempts})"),
                metadata={
                    "dialect": inputs.dialect,
                    "eligible_module_count": len(inputs.eligible_units),
                    "attempt": attempt,
                },
            )
        except Exception as exc:
            feedback = [f"OVERVIEW generation command failed: {exc}"]
            logging.warning(
                "Chip Overview: generation attempt %d/%d failed: %s",
                attempt,
                attempts,
                exc,
            )
            continue

        feedback = _publish_candidate(pending_path, final_path, inputs)
        if not feedback:
            logging.info("Chip Overview: published generated output to %s", final_path)
            return
        logging.warning(
            "Chip Overview: candidate attempt %d/%d rejected: %s",
            attempt,
            attempts,
            "; ".join(feedback),
        )

    raise RuntimeError(
        "chip Overview generation failed after "
        f"{attempts} attempt(s): {'; '.join(feedback)}"
    )
