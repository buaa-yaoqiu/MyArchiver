"""Explain sync effects using the same adapters against an isolated archive."""
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from .importers import substantive_metadata
from .store import Archive, canonical, csv_bytes, METRIC_FIELDS, COMPARISON_FIELDS


def classify(previous, metadata, note, metrics, comparisons):
    if previous is None:
        return ["new_record"]
    changes = []
    if (csv_bytes(previous["metrics"], METRIC_FIELDS) != csv_bytes(metrics or [], METRIC_FIELDS)
            or csv_bytes(previous["comparisons"], COMPARISON_FIELDS) != csv_bytes(comparisons or [], COMPARISON_FIELDS)):
        changes.append("metrics")
    if previous["note"].rstrip() != note.rstrip():
        changes.append("body")
    old = previous["metadata"]
    if canonical({k: v for k, v in old.items() if k != "source_refs"}) != canonical({k: v for k, v in metadata.items() if k != "source_refs"}):
        changes.append("metadata")
    if old.get("source_refs") != metadata.get("source_refs"):
        changes.append("location_only" if substantive_metadata(old).get("source_refs") == substantive_metadata(metadata).get("source_refs") else "sources")
    return changes or ["unchanged"]


class PlanningArchive(Archive):
    def __init__(self, root):
        super().__init__(root)
        self.effects = []

    def observe_import(self, previous, metadata, note, metrics, comparisons, reused):
        if reused:
            self.effects.append({"experiment_id": metadata["experiment_id"], "changes": classify(previous, metadata, note, metrics, comparisons), "action": "reuse"})

    def publish(self, metadata, note, metrics=None, comparisons=None, reason=None):
        identity = metadata["experiment_id"]
        previous = self.record(identity) if identity in self.manifest()["experiments"] else None
        result = super().publish(metadata, note, metrics, comparisons, reason)
        current = self.record(identity)
        self.effects.append({"experiment_id": identity, "changes": classify(previous, current["metadata"], note, metrics, comparisons),
                             "action": "new" if previous is None else "update" if result["changed"] else "reuse"})
        return result


def preview(archive, inputs, options, load_adapter):
    manifest, snapshot = archive.snapshot() if (archive.root / "manifest.json").exists() else (None, None)
    with tempfile.TemporaryDirectory(prefix="ea-preview-") as temporary:
        clone = PlanningArchive(Path(temporary) / "archive")
        clone.init()
        manifest = manifest or clone.manifest()
        from .store import atomic_write
        for entry in manifest["experiments"].values():
            shutil.copytree(archive.root / entry["path"], clone.root / entry["path"])
        atomic_write(clone.root / "manifest.json", canonical(manifest))
        tasks = []
        for source, path, _ in inputs:
            clone.effects = []
            adapter = load_adapter(source.get("adapter", source.get("type")))
            adapter.ingest(clone, path, options(source), "Sync preview")
            tasks.append({"id": source["id"], "records": clone.effects,
                          "counts": dict(Counter(effect["action"] for effect in clone.effects))})
        final = clone.manifest()
        touched = {effect["experiment_id"] for task in tasks for effect in task["records"]}
        counts = Counter()
        for identity in touched:
            old = manifest["experiments"].get(identity)
            counts["new" if old is None else "update" if old["content_sha256"] != final["experiments"][identity]["content_sha256"] else "reuse"] += 1
    return {"archive_snapshot_sha256": snapshot, "details": tasks,
            "counts": dict(counts), "predicted_revisions": final["generation"] - manifest["generation"],
            "operation_counts": dict(sum((Counter(task["counts"]) for task in tasks), Counter()))}
