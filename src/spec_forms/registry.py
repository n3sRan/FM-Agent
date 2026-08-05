"""Registry of built-in specification forms."""

from __future__ import annotations

from types import MappingProxyType

from .base import SpecForm
from .software import SOFTWARE_SPEC_FORM


_SPEC_FORMS = MappingProxyType({
    SOFTWARE_SPEC_FORM.id: SOFTWARE_SPEC_FORM,
})


def get_spec_form(form_id: str) -> SpecForm:
    """Return a registered specification form or reject the unknown id."""
    try:
        return _SPEC_FORMS[form_id]
    except KeyError:
        supported = ", ".join(sorted(_SPEC_FORMS))
        raise ValueError(
            f"Unknown specification form {form_id!r}; supported forms: {supported}"
        ) from None


def supported_spec_forms() -> tuple[str, ...]:
    """Return registered form ids in stable order."""
    return tuple(sorted(_SPEC_FORMS))
