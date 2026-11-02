"""One local acceptance receipt; never substitutes for an experimental audit."""
from pathlib import Path

from .search import SearchIndex
from .store import atomic_write, canonical, file_sha, now, compare_definitions, ArchiveError, read_json, inside


def accept(archive):
    try:
        report = _accept(archive)
    except (ArchiveError, OSError, ValueError) as exc:
        report = {"completed_at": now(), "status": "failed", "issues": [{"check": "archive_or_index_integrity", "error": str(exc)}],
                  "scope": "Local acceptance failed before all checks completed; see issue. No experimental audit performed."}
    atomic_write(archive.root / ".cache/acceptance-receipt.json", canonical(report))
    return report


def _accept(archive):
    manifest, snapshot = archive.snapshot()
    verification = archive.verify()
    issues, sources, pairings, completeness = [], [], [], []
    identities = {}
    for record in archive.records(manifest):
        metadata = record["metadata"]
        identity = metadata["experiment_id"]
        target = metadata.get("canonical_experiment_id")
        if target and (target not in manifest["experiments"] or archive.record(target, manifest)["metadata"].get("canonical_experiment_id")):
            issues.append({"id": identity, "check": "canonical_identity", "status": "invalid"})
        explicit = metadata.get("experiment_identity")
        if explicit:
            key = canonical(explicit).decode("utf-8")
            primary = target or identity
            if key in identities and identities[key] != primary:
                issues.append({"id": identity, "check": "duplicate_explicit_identity", "other": identities[key]})
            identities[key] = primary
        for source in metadata.get("source_refs", []):
            path = Path(source["path"]) if source.get("path") else None
            status = "not_verifiable"
            if path and path.is_file() and source.get("sha256"):
                status = "match" if file_sha(path) == source["sha256"] else "source_changed_since_snapshot"
            sources.append({"id": identity, "source": source["id"], "status": status})
        evaluations = metadata.get("evaluations", {})
        pairs = set()
        for row in record["comparisons"]:
            key = (row["candidate_evaluation_id"], row["baseline_evaluation_id"])
            if key not in pairs:
                pairs.add(key)
                pairings.append({"id": identity, "candidate": key[0], "baseline": key[1],
                                 **compare_definitions(evaluations[key[0]], evaluations[key[1]])})
        expected = metadata.get("completeness", {})
        actual = {"metric_rows": len(record["metrics"]), "comparison_rows": len(record["comparisons"]), "evaluations": len(evaluations)}
        for key, count in expected.items():
            if key in actual and actual[key] != count:
                issues.append({"id": identity, "check": "completeness", "field": key, "expected": count, "actual": actual[key]})
        completeness.append({"id": identity, "status": "checked" if expected else "not_declared", "expected": expected, "actual": actual})
    if archive.snapshot()[1] != snapshot:
        raise ArchiveError("Archive changed during acceptance; retry")
    index = SearchIndex(archive).status()
    if index.get("exists"):
        import numpy as np
        folder = inside(archive.root, read_json(archive.root / ".cache/index.json")["path"])
        for name, expected_sha in index["files"].items():
            if file_sha(inside(folder, name)) != expected_sha:
                raise ArchiveError(f"Index file hash mismatch: {name}")
        chunks = read_json(folder / "chunks.json")
        matrix = np.load(folder / "embeddings.npy", allow_pickle=False)
        if (matrix.shape != (len(chunks), index["model"]["dimension"]) or not np.isfinite(matrix).all()
                or not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-3)):
            raise ArchiveError("Invalid index dimensions or non-finite vectors")
        index["integrity"] = "pass"
    warnings = sum(s["status"] != "match" for s in sources) + sum(p["status"] != "compatible" for p in pairings) + sum(c["status"] == "not_declared" for c in completeness) + int(not index.get("exists") or index.get("stale", True))
    report = {"completed_at": now(), "archive_snapshot_sha256": snapshot,
              "status": "failed" if issues else "needs_review" if warnings else "pass", "issues": issues,
              "verification": verification, "source_hashes": sources, "baseline_pairs": pairings,
              "completeness": completeness, "index": index, "warnings": warnings,
              "scope": "Local archive schema, hashes, evaluation references, explicit identities, declared completeness and index status. Source drift is reported, not overwritten. No remote rerun or scientific conclusion verification."}
    return report
