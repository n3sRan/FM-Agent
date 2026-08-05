"""Specification-form contracts and built-in implementations."""

from .base import SpecArtifactPaths, SpecForm, SpecValidationResult
from .registry import get_spec_form, supported_spec_forms
from .software import SOFTWARE_SPEC_FORM, SoftwareSpecForm

__all__ = [
    "SOFTWARE_SPEC_FORM",
    "SoftwareSpecForm",
    "SpecArtifactPaths",
    "SpecForm",
    "SpecValidationResult",
    "get_spec_form",
    "supported_spec_forms",
]
