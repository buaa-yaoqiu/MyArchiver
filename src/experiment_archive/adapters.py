"""Source adapter port and built-ins. Optional plugins use Python entry points."""
from __future__ import annotations

import inspect
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from .importers import import_markdown, publish_import
from .store import (Archive, ArchiveError, COMPARISON_FIELDS, METRIC_FIELDS, file_sha,
                    read_csv, read_json)


class SourceAdapter(Protocol):
    api_version: int

    def inputs(self, path: Path, options: dict) -> list[Path]:
        """Declare every source file read during ingest, for preflight/change detection."""
        ...

    def ingest(self, archive: Archive, path: Path, options: dict, reason: str) -> dict:
        """Read project formats and publish normalized records; return records_processed."""
        ...


class MarkdownAdapter:
    api_version = 1

    def inputs(self, path: Path, options: dict) -> list[Path]:
        return [path]

    def ingest(self, archive: Archive, path: Path, options: dict, reason: str) -> dict:
        result = import_markdown(archive, path, options.get("split_sections", False),
                                 options.get("experiment_id"), reason,
                                 domain_terms=options.get("domain_terms", []),
                                 metadata_defaults=options.get("metadata_defaults"))
        return {"records_processed": result["imported"], "changed": result["changed"]}


class BundleAdapter:
    """Project exports the four standard files; no custom Python plugin is needed."""
    api_version = 1

    def inputs(self, path: Path, options: dict) -> list[Path]:
        return [path / name for name in ("metadata.json", "note.md", "metrics.csv", "comparisons.csv")]

    def ingest(self, archive: Archive, path: Path, options: dict, reason: str) -> dict:
        result = publish_import(archive, read_json(path / "metadata.json"),
            (path / "note.md").read_text(encoding="utf-8"),
            read_csv(path / "metrics.csv", METRIC_FIELDS),
            read_csv(path / "comparisons.csv", COMPARISON_FIELDS), reason)
        return {"records_processed": 1, "changed": result["changed"]}


def load_adapter(name: str) -> SourceAdapter:
    builtins = {"markdown": MarkdownAdapter, "bundle": BundleAdapter}
    if name in builtins:
        return builtins[name]()
    candidates = list(entry_points(group="experiment_archive.sources", name=name))
    if not candidates:
        raise ArchiveError(f"Source adapter {name!r} is not installed. Install its integration package, or use markdown/bundle.")
    if len(candidates) != 1:
        raise ArchiveError(f"Ambiguous installed source adapter {name!r}")
    adapter = candidates[0].load()()
    if getattr(adapter, "api_version", None) != 1 or not all(callable(getattr(adapter, method, None)) for method in ("inputs", "ingest")):
        raise ArchiveError(f"Adapter {name!r} does not implement SourceAdapter v1")
    return adapter


def adapter_info(name: str, adapter: SourceAdapter) -> dict:
    implementation = inspect.getsourcefile(type(adapter))
    return {"name": name, "api_version": adapter.api_version,
            "implementation_sha256": file_sha(Path(implementation)) if implementation else None}


def list_adapters() -> list[str]:
    return sorted({"markdown", "bundle", *(ep.name for ep in entry_points(group="experiment_archive.sources"))})


def ingest_source(archive: Archive, name: str, path: Path, options: dict | None = None,
                  reason: str = "Explicit adapter import") -> dict:
    adapter, options = load_adapter(name), options or {}
    paths = adapter.inputs(path.resolve(), options)
    missing = [str(item) for item in paths if not item.is_file()]
    if missing:
        raise ArchiveError(f"Missing adapter inputs: {missing}")
    return adapter.ingest(archive, path.resolve(), options, reason)
