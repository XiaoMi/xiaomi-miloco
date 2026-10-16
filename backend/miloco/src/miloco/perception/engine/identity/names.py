"""Shared person-name policy without library I/O."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miloco.perception.engine.identity.library import PersonRef


def usable_person_name(name: str | None) -> str | None:
    """Reject absent or whitespace-only names; preserve valid spelling."""
    if not name or not name.strip():
        return None
    return name


def build_name_lookup(persons: Iterable[PersonRef]) -> dict[str, str]:
    """Build display names from an already-read library snapshot."""
    return {
        ref.person_id: name
        for ref in persons
        if (name := usable_person_name(ref.name)) is not None
    }
