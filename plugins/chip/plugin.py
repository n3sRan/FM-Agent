"""Configure the public Pipeline with the detected chip Profile."""

from src.specification import configure_specification

from plugins.chip.detection import detect_chip_context, read_plugin_submodules
from plugins.chip.eligibility import prepare_spec_generation as prepare_eligibility
from plugins.chip.project_overview import (
    generate_project_overview_document,
    prepare_project_overview,
)
from plugins.chip.profiles import PROFILES


def configure(proj_dir: str) -> None:
    """Detect the invocation's hardware dialect and register its Profile."""
    submodules = read_plugin_submodules(proj_dir)
    context = detect_chip_context(proj_dir, submodules=submodules)
    configure_specification(PROFILES[context.dialect])


def prepare_spec_generation(proj_dir: str) -> None:
    """Prepare the run-level Overview before existing Stage 6 eligibility work."""
    prepare_project_overview(proj_dir)
    prepare_eligibility(proj_dir)


def generate_project_overview(proj_dir: str) -> None:
    """Publish the chip run's Overview after all module artifacts are ready."""
    generate_project_overview_document(proj_dir)
