"""Metric semantics belong to record definitions, never to a project name."""
from __future__ import annotations

from .store import ArchiveError


def normalize_metric(name: str | None, metadata: dict) -> str | None:
    if name is None:
        return None
    definitions = metadata.get("metric_definitions", {})
    if name in definitions:
        return name
    found = [key for key, value in definitions.items() if name in value.get("aliases", [])]
    if len(found) > 1:
        raise ArchiveError(f"Ambiguous metric alias {name!r}: {found}")
    return found[0] if found else name


def metric_direction(name: str, metadata: dict) -> str | None:
    return metadata.get("metric_definitions", {}).get(name, {}).get("direction")


def percentage_points(name: str, metadata: dict) -> bool:
    return metadata.get("metric_definitions", {}).get(name, {}).get("percentage_points", False) is True


def validate_definitions(metadata: dict) -> None:
    definitions = metadata.get("metric_definitions", {})
    if not isinstance(definitions, dict):
        raise ArchiveError("metric_definitions must be an object")
    alias_owner = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not isinstance(definition, dict):
            raise ArchiveError("Invalid metric definition")
        if definition.get("direction") not in (None, "minimize", "maximize"):
            raise ArchiveError(f"Invalid metric direction for {name}")
        aliases = definition.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise ArchiveError(f"Metric aliases must be strings: {name}")
        if "percentage_points" in definition and not isinstance(definition["percentage_points"], bool):
            raise ArchiveError("percentage_points must be boolean")
        for alias in [name, *aliases]:
            if alias in alias_owner and alias_owner[alias] != name:
                raise ArchiveError(f"Conflicting metric alias: {alias}")
            alias_owner[alias] = name
    search_aliases = metadata.get("search_aliases", {})
    if not isinstance(search_aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in search_aliases.items()):
        raise ArchiveError("search_aliases must be a string-to-string object")
