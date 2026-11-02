"""Project-independent import helpers. Project adapters live in separate packages."""
from __future__ import annotations

import re
from pathlib import Path

from .store import Archive, ArchiveError, digest, file_sha, canonical, csv_bytes, METRIC_FIELDS, COMPARISON_FIELDS


def substantive_metadata(metadata):
    """Ignore relocations only when a section content hash is available."""
    result = {**metadata, "source_refs": []}
    for source in metadata.get("source_refs", []):
        if source.get("section_sha256"):
            source = {key: value for key, value in source.items()
                      if key not in ("sha256", "start_line", "end_line")}
        result["source_refs"].append(source)
    return result


def source_ref(path: Path, source_id: str, text: str | None = None, heading: str | None = None) -> dict:
    result = {"id": source_id, "path": str(path.resolve()), "sha256": file_sha(path)}
    if text is not None:
        result["section_sha256"] = digest(text.encode("utf-8"))
    if heading:
        result["heading"] = heading
    return result


def publish_import(archive: Archive, metadata: dict, note: str, metrics=None, comparisons=None,
                   reason: str | None = None) -> dict:
    """Preserve recorded review decisions and custom properties on source refresh."""
    identity = metadata["experiment_id"]
    if (archive.root / "manifest.json").exists() and identity in archive.manifest()["experiments"]:
        previous = archive.record(identity)
        old = previous["metadata"]
        for field in ("claim_status", "evidence_validity", "canonical_experiment_id", "identity_link_basis"):
            if field in old:
                metadata[field] = old[field]
        def merge(previous, imported):
            if not isinstance(previous, dict) or not isinstance(imported, dict):
                return imported
            return {**previous, **{key: merge(previous.get(key), value) for key, value in imported.items()}}
        metadata["attributes"] = merge(old.get("attributes", {}), metadata.get("attributes", {}))
        def signatures(value):
            return {s["id"]: s.get("section_sha256", s.get("sha256")) for s in value.get("source_refs", [])}
        if signatures(old) != signatures(metadata) and metadata.get("evidence_validity") == "valid":
            metadata["evidence_validity"] = "needs_review"
        if (canonical(substantive_metadata(old)) == canonical(substantive_metadata(metadata))
                and previous["note"].rstrip() == note.rstrip()
                and csv_bytes(previous["metrics"], METRIC_FIELDS) == csv_bytes(metrics or [], METRIC_FIELDS)
                and csv_bytes(previous["comparisons"], COMPARISON_FIELDS) == csv_bytes(comparisons or [], COMPARISON_FIELDS)):
            if hasattr(archive, "observe_import"):
                archive.observe_import(previous, metadata, note, metrics, comparisons, True)
            return {"experiment_id": identity, "revision": previous["revision"], "changed": False,
                    "reuse_reason": "section content unchanged; archived source snapshot retained"}
    return archive.publish(metadata, note, metrics, comparisons, reason)


def markdown_sections(path: Path, level: int = 2) -> list[tuple[str, str, int, int]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    starts = [(i, line.lstrip("#").strip()) for i, line in enumerate(lines)
              if re.match(r"^" + "#" * level + r"\s+", line)]
    return [(title, "".join(lines[start:end]), start + 1, end)
            for (start, title), end in zip(starts, [s[0] for s in starts[1:]] + [len(lines)])]


def import_markdown(archive: Archive, path: Path, split: bool = False,
                    experiment_id: str | None = None, reason: str | None = None,
                    *, domain_terms: tuple | list = (), metadata_defaults: dict | None = None) -> dict:
    if not path.is_file():
        raise ArchiveError(f"Missing source document: {path}")
    sections = markdown_sections(path) if split else [(path.stem, path.read_text(encoding="utf-8-sig"), 1, None)]
    if split and experiment_id:
        raise ArchiveError("--id cannot be used with --split-sections")
    output, ids = [], set()
    namespace = digest(str(path.resolve()).casefold().encode())[:8]
    for title, text, start, end in sections:
        identity = experiment_id or "note_" + namespace + "_" + digest(title.encode())[:16]
        if identity in ids:
            raise ArchiveError(f"Duplicate section title needs explicit identity: {title}")
        ids.add(identity)
        source = source_ref(path, "original", text, title)
        source.update(start_line=start, end_line=end)
        metadata = {**(metadata_defaults or {}), "schema_version": 1, "experiment_id": identity, "title": title,
            "kind": "historical_note", "domains": [d for d in domain_terms if d.casefold() in text.casefold()],
            "attributes": {}, "evidence_validity": "unknown", "claim_status": "current", "audit_status": "unknown",
            "verification_basis": "unreviewed_original_text_snapshot", "evidence_role": "unknown",
            "source_refs": [source], "evaluations": {}}
        metadata.setdefault("limitations", ["历史原文未复核，可能含过期状态或后续更正；未提取或确认科学指标。"])
        warning = "> 导入身份：未复核历史原文。可能含执行中状态或后续已更正的结论；不能直接当作当前有效成绩。\n\n"
        output.append(publish_import(archive, metadata, warning + text, reason=reason))
    return {"imported": len(output), "changed": sum(r["changed"] for r in output),
            "records": output[:10], "records_truncated": len(output) > 10}
