"""Abstract contract for one specification artifact form."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SpecArtifactPaths:
    """The self-spec and dependency-info artifacts for one analysis unit."""

    self_spec: Path
    dependency_info: Path


@dataclass(frozen=True)
class SpecValidationResult:
    """Side-effect-free result of checking one unit's spec artifacts."""

    ready: bool
    errors: tuple[str, ...] = ()


class SpecForm(ABC):
    """Describe how Stage 6 writes, reads, and validates specifications.

    A specification form deliberately owns no project-analysis, extraction,
    dependency-discovery, reasoning, or candidate-validation behavior.
    """

    id: str

    @abstractmethod
    def system_prompt_path(self, script_dir: Path) -> Path:
        """Return the repository source path of this form's system prompt."""

    @abstractmethod
    def workflow_prompt_path(self, script_dir: Path) -> Path:
        """Return the repository source path of this form's batch workflow."""

    @abstractmethod
    def artifact_paths(self, unit_file: Path) -> SpecArtifactPaths:
        """Return the artifacts adjacent to ``unit_file``."""

    @abstractmethod
    def validate(
        self,
        unit_file: Path,
        expected_dependencies: Sequence[str] = (),
    ) -> SpecValidationResult:
        """Check whether the unit's artifacts are complete without mutating them."""

    @abstractmethod
    def read_self_spec(self, unit_file: Path) -> str | None:
        """Return caller-facing self-spec context, or ``None`` when unreadable."""

    @abstractmethod
    def read_dependency_expectation(
        self,
        caller_file: Path,
        callee_fqn: str,
        aliases: Sequence[str] = (),
    ) -> str | None:
        """Return one caller's expectation for a callee, if recorded."""

    @abstractmethod
    def batch_intro(self, language: str) -> str:
        """Render the form-specific introduction for a generated batch prompt."""

    @abstractmethod
    def output_contract_prompt(self) -> str:
        """Render the form-specific output contract for a batch prompt."""

    @abstractmethod
    def generation_instruction(self, batch_prompt_rel: str, attempt: int) -> str:
        """Render the instruction used to launch one spec-generation agent."""

    @abstractmethod
    def trace_outputs(self, unit_files: Sequence[str]) -> list[str]:
        """Return project-relative output paths expected from a batch."""
