"""Public integration API. Projects need not import CLI or storage internals."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .adapters import ingest_source
from .importers import publish_import
from .metrics import normalize_metric
from .pipeline import evidence, sync
from .store import Archive


@dataclass
class ExperimentRecord:
    """Normalized write contract, also serializable as the four-file bundle."""
    metadata: dict
    note: str
    metrics: list[dict] = field(default_factory=list)
    comparisons: list[dict] = field(default_factory=list)

    def write_bundle(self, destination: str | Path) -> Path:
        """Export the same structured result used by a project report, without parsing Markdown."""
        from .store import validate, atomic_write, canonical, csv_bytes, METRIC_FIELDS, COMPARISON_FIELDS
        validate(self.metadata, self.metrics, self.comparisons)
        payload = {"metadata.json": canonical(self.metadata), "note.md": (self.note.rstrip() + "\n").encode("utf-8"),
                   "metrics.csv": csv_bytes(self.metrics, METRIC_FIELDS), "comparisons.csv": csv_bytes(self.comparisons, COMPARISON_FIELDS)}
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=False)
        for name, data in payload.items():
            atomic_write(path / name, data)
        return path


class ArchiveClient:
    """Explicit archive path; no default project paths and no network service."""

    def __init__(self, root: str | Path):
        self.archive = Archive(root)

    def publish(self, record: ExperimentRecord, *, reason: str | None = None) -> dict:
        return self.archive.publish(record.metadata, record.note, record.metrics, record.comparisons, reason)

    def import_record(self, record: ExperimentRecord, *, reason: str) -> dict:
        """Refresh source-derived data while preserving retractions and review decisions."""
        return publish_import(self.archive, record.metadata, record.note, record.metrics, record.comparisons, reason)

    def ingest(self, path: str | Path, *, adapter: str = "bundle", options: dict | None = None,
               reason: str = "Project export") -> dict:
        return ingest_source(self.archive, adapter, Path(path), options, reason)

    def sync(self, config: str | Path, **options) -> dict:
        return sync(self.archive, Path(config), **options)

    def evidence(self, query: str, **filters) -> dict:
        target = filters.pop("language", None)
        provider = filters.pop("translation_provider", None)
        result = evidence(self.archive, query, **filters)
        if target:
            from .translation import TranslationService
            result = TranslationService(self.archive).evidence(result, target, provider)
        return result

    def translate(self, experiment_id: str, *, language: str = "zh-CN", provider: str | None = None,
                  revision: str | None = None) -> dict:
        from .translation import TranslationService
        return TranslationService(self.archive).record(experiment_id, language, provider, revision)

    def read(self, experiment_id: str, *, revision: str | None = None) -> dict:
        return self.archive.record(experiment_id, revision=revision)

    def table(self, experiment_id: str, *, table: str = "metrics", metric: str | None = None,
              domain: str | None = None, scope_type: str | None = None,
              limit: int = 20, offset: int = 0) -> dict:
        if table not in ("metrics", "comparisons") or not 1 <= limit <= 1000 or offset < 0:
            from .store import ArchiveError
            raise ArchiveError("Invalid table or pagination")
        record = self.archive.record(experiment_id)
        selected_metric = normalize_metric(metric, record["metadata"])
        rows = [row for row in record[table] if (not selected_metric or row["metric"] == selected_metric)
                and (not domain or row["domain"] == domain) and (not scope_type or row["scope_type"] == scope_type)]
        return {"revision": record["revision"], "total": len(rows), "rows": rows[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None}

    def verify(self) -> dict:
        return self.archive.verify()

    def accept(self) -> dict:
        from .acceptance import accept
        return accept(self.archive)
