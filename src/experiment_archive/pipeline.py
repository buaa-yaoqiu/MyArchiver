"""Deterministic local maintenance and bounded evidence collection, without an LLM."""
from __future__ import annotations

import copy
import math
import time
from contextlib import contextmanager
from pathlib import Path

from .adapters import adapter_info, load_adapter
from .metrics import metric_direction, normalize_metric
from .search import SearchIndex
from .store import Archive, ArchiveError, atomic_write, canonical, digest, file_sha, now, read_json


def local_embedder(archive: Archive, threads: int = 2, batch_size: int = 4):
    from .embedding import E5Embedder
    return E5Embedder(archive.root / ".cache/models/multilingual-e5-small", threads, batch_size)


def load_config(path: Path) -> dict:
    config = read_json(path)
    if config.get("schema_version") != 1 or not isinstance(config.get("sources"), list):
        raise ArchiveError("Pipeline requires schema_version=1 and sources array")
    ids = set()
    for source in config["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("adapter", source.get("type")), str):
            raise ArchiveError("Source requires an adapter name")
        if "adapter" in source and "type" in source and source["adapter"] != source["type"]:
            raise ArchiveError("Conflicting adapter and legacy type names")
        if not isinstance(source.get("options", {}), dict):
            raise ArchiveError("Source options must be an object")
        identity = source.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ArchiveError("Pipeline source IDs must be nonempty and unique")
        ids.add(identity)
        if not isinstance(source.get("path"), str) or not source["path"]:
            raise ArchiveError(f"Source {identity} requires a path")
        for flag in ("split_sections", "include_log"):
            if flag in source and not isinstance(source[flag], bool):
                raise ArchiveError(f"Source {identity}: {flag} must be boolean")
    return config


def source_options(source: dict) -> dict:
    # Flat source options remain supported for older pipeline.json files.
    return {**{k: v for k, v in source.items() if k not in ("id", "type", "adapter", "path", "options")},
            **source.get("options", {})}


def source_state(source: dict, config_dir: Path) -> tuple[Path, dict]:
    path = Path(source["path"])
    if not path.is_absolute():
        path = config_dir / path
    path = path.resolve()
    name = source.get("adapter", source.get("type"))
    adapter = load_adapter(name)
    paths = adapter.inputs(path, source_options(source))
    missing = [str(item) for item in paths if not item.is_file()]
    if missing:
        raise ArchiveError(f"Missing pipeline input(s): {missing}")
    state = {"source": source, "resolved_path": str(path), "adapter": adapter_info(name, adapter),
             "files": {str(item): file_sha(item) for item in paths}}
    return path, state


@contextmanager
def pipeline_lock(archive: Archive):
    # Use the same exclusive lock primitive, in a separate namespace; individual
    # publish/index operations still take the archive writer lock themselves.
    lock = Archive(archive.root / ".cache/pipeline-lock")
    with lock.writer():
        yield


def sync(archive: Archive, config_path: Path, *, threads: int = 2, batch_size: int = 4,
         dry_run: bool = False, index_mode: str = "semantic", embedder_factory=None) -> dict:
    started = time.perf_counter()
    config_path = config_path.resolve()
    config = load_config(config_path)
    if index_mode not in ("semantic", "none"):
        raise ArchiveError("index mode must be semantic or none")
    # Preflight every input before starting any publication; missing files do not
    # silently delete records, and a broken task cannot masquerade as success.
    inputs = [(source, *source_state(source, config_path.parent)) for source in config["sources"]]
    receipt_path = archive.root / ".cache/pipeline_receipt.json"
    previous = read_json(receipt_path) if receipt_path.exists() else {}
    previous_sources = previous.get("sources", {})
    current_manifest = archive.manifest() if (archive.root / "manifest.json").exists() else None
    manifest_unchanged = bool(current_manifest and previous.get("archive_snapshot_sha256") == digest(canonical(current_manifest)))
    plan = [{"id": source["id"], "adapter": source.get("adapter", source.get("type")), "path": str(path),
             "changed": previous_sources.get(source["id"]) != state or not manifest_unchanged}
            for source, path, state in inputs]
    removed = sorted(set(previous_sources) - {source["id"] for source, _, _ in inputs})
    if dry_run:
        from .changes import preview
        details = preview(archive, inputs, source_options, load_adapter)
        if any(source_state(source, config_path.parent)[1] != state for source, _, state in inputs):
            raise ArchiveError("Inputs changed during preview; rerun preview")
        if current_manifest and archive.snapshot()[1] != details["archive_snapshot_sha256"]:
            raise ArchiveError("Archive changed during preview; rerun preview")
        return {"dry_run": True, "config": str(config_path), "tasks": plan,
                **details, "removed_sources_kept": removed,
                "notice": "Adapters simulated in a temporary archive; original sources, archive and models unchanged."}
    factory = embedder_factory or (lambda: local_embedder(archive, threads, batch_size))
    model = None
    if index_mode == "semantic":
        # Fail early on missing model/dependencies; never silently download.
        model = factory()
    archive.init()
    with pipeline_lock(archive):
        archive.verify()
        tasks = []
        # Re-evaluate the last successful receipt after obtaining the pipeline lock.
        previous = read_json(receipt_path) if receipt_path.exists() else {}
        manifest_unchanged = previous.get("archive_snapshot_sha256") == archive.snapshot()[1]
        for source, path, state in inputs:
            unchanged = manifest_unchanged and previous.get("sources", {}).get(source["id"]) == state
            if unchanged:
                tasks.append({"id": source["id"], "action": "skipped_unchanged"})
                continue
            before = archive.manifest()["generation"]
            reason = f"Scripted sync: {source['id']}; input SHA changed or receipt absent"
            adapter = load_adapter(source.get("adapter", source.get("type")))
            summary = adapter.ingest(archive, path, source_options(source), reason)
            count = summary["records_processed"]
            tasks.append({"id": source["id"], "action": "imported", "records_processed": count,
                          "revisions_published": archive.manifest()["generation"] - before})
        # A source edited during import requires another sync; never stamp the
        # earlier SHA as if all output came from the same unchanged input set.
        for source, _, state in inputs:
            if source_state(source, config_path.parent)[1] != state:
                raise ArchiveError(f"Input changed while syncing: {source['id']}. Published revisions remain; rerun sync.")
        verification = archive.verify()
        index = SearchIndex(archive)
        if index_mode == "semantic":
            status = index.status()
            if not status["exists"] or status["stale"] or status.get("model", {}).get("fingerprint") != model.fingerprint:
                index_report = {"action": "rebuilt", **index.build(model)}
            else:
                index_report = {"action": "skipped_current", "chunks": status["chunks"], "encoded": 0}
        else:
            index_report = {"action": "not_requested", **index.status()}
        snapshot_sha = archive.snapshot()[1]
        receipt = {"schema_version": 1, "completed_at": now(), "config_path": str(config_path),
                   "config_sha256": file_sha(config_path), "archive_snapshot_sha256": snapshot_sha,
                   "sources": {source["id"]: state for source, _, state in inputs},
                   "tasks": tasks, "verification": verification, "index": index_report,
                   "removed_sources_kept": removed, "seconds": time.perf_counter() - started}
        atomic_write(receipt_path, canonical(receipt))
        return {key: value for key, value in receipt.items() if key != "sources"}


def evidence(archive: Archive, query: str, *, mode: str = "hybrid", domain: str | None = None,
             metric: str | None = None, scope_type: str = "domain", limit: int = 3, rows_per_record: int = 8,
             include_retracted: bool = False, threads: int = 2, batch_size: int = 4,
             refresh_index: bool = True, embedder_factory=None, sort: str = "relevance") -> dict:
    if not 1 <= rows_per_record <= 50:
        raise ArchiveError("rows_per_record must be 1..50")
    index = SearchIndex(archive)
    model, index_action = None, "not_used"
    if mode != "lexical":
        factory = embedder_factory or (lambda: local_embedder(archive, threads, batch_size))
        model = factory()
        status = index.status()
        if not status["exists"] or status["stale"] or status.get("model", {}).get("fingerprint") != model.fingerprint:
            if not refresh_index:
                raise ArchiveError("Index missing/stale/different model; run index or allow automatic refresh")
            index.build(model)
            index_action = "refreshed"
        else:
            index_action = "current"
    result = index.search(query, model, mode=mode, domain=domain, limit=limit,
                          include_retracted=include_retracted, max_chars=450, sort=sort)
    manifest = archive.manifest()
    if manifest["generation"] != result["archive_generation"]:
        raise ArchiveError("Archive changed while collecting evidence; retry")
    for hit in result["results"]:
        record = archive.record(hit["experiment_id"], manifest)
        metadata = record["metadata"]
        selected_metric = normalize_metric(metric, metadata)
        selected = [row for row in record["metrics"] if (not selected_metric or row["metric"] == selected_metric)
                    and (not domain or row["domain"].casefold() == domain.casefold())
                    and (scope_type == "all" or row["scope_type"] == scope_type)]
        comparisons = [row for row in record["comparisons"] if (not selected_metric or row["metric"] == selected_metric)
                       and (not domain or row["domain"].casefold() == domain.casefold())
                       and (scope_type == "all" or row["scope_type"] == scope_type)]
        hit["metric_rows_total"] = len(selected)
        hit["metric_rows"] = selected[:rows_per_record]
        hit["comparison_rows_total"] = len(comparisons)
        hit["comparison_rows"] = []
        for original in comparisons[:rows_per_record]:
            row = copy.deepcopy(original)
            candidate = metadata["evaluations"][row["candidate_evaluation_id"]]
            baseline = metadata["evaluations"][row["baseline_evaluation_id"]]
            row["candidate_variant"] = candidate["variant_id"]
            row["baseline_variant"] = baseline["variant_id"]
            row["numeric_direction"], row["ci_direction"] = direction(row, metadata)
            hit["comparison_rows"].append(row)
        evaluation_ids = {row["evaluation_id"] for row in selected[:rows_per_record]}
        for row in comparisons[:rows_per_record]:
            evaluation_ids.update((row["candidate_evaluation_id"], row["baseline_evaluation_id"]))
        hit["evaluation_context"] = {eid: metadata["evaluations"][eid] for eid in sorted(evaluation_ids)}
        hit["limitations"] = metadata.get("limitations", [])
        hit["table_next_step"] = (f"metrics/comparisons {hit['experiment_id']} --offset {rows_per_record}"
                                  if len(selected) > rows_per_record or len(comparisons) > rows_per_record else None)
    result["index_action"] = index_action
    result["metric_filter"], result["scope_filter"] = metric, scope_type
    result["notice"] = ("Script collects evidence and labels numeric/CI directions only. Scientific benefit, protocol equivalence, "
                        "causal attribution and review status must be interpreted from the attached context. "
                        "Source import is explicit via sync; evidence refreshes the index, not source documents.")
    return result


def direction(row: dict, metadata: dict | None = None) -> tuple[str, str]:
    metric = row["metric"]
    preference = metric_direction(metric, metadata or {})
    if preference not in ("minimize", "maximize"):
        return "unknown_metric_direction", "unknown_metric_direction"
    sign = -1 if preference == "maximize" else 1
    delta = float(row["delta"]) * sign
    numeric = "improved" if delta < 0 else "worsened" if delta > 0 else "unchanged"
    if row.get("ci_low") in (None, "") or row.get("ci_high") in (None, ""):
        return numeric, "not_available"
    low, high = sorted((float(row["ci_low"]) * sign, float(row["ci_high"]) * sign))
    if not all(math.isfinite(value) for value in (delta, low, high)):
        raise ArchiveError("Non-finite comparison in evidence")
    return numeric, "improved" if high < 0 else "worsened" if low > 0 else "includes_zero_not_equivalence"
