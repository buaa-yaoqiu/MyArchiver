from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ArchiveError(Exception):
    """An actionable archive validation or consistency error."""


METRIC_FIELDS = ["experiment_id", "evaluation_id", "variant_id", "domain", "scope_type",
                 "scope_id", "prediction_phase", "metric", "value", "unit", "n_frames",
                 "value_status", "source_ref"]
COMPARISON_FIELDS = ["experiment_id", "candidate_evaluation_id", "baseline_evaluation_id",
                     "domain", "scope_type", "scope_id", "metric", "delta", "relative_percent",
                     "percentage_points", "ci_low", "ci_high", "compatibility", "source_ref"]
FILES = ("note.md", "metadata.json", "metrics.csv", "comparisons.csv")
COMPATIBILITY_KEYS = ("dataset_version", "input_signature", "scoring_signature",
                      "frame_manifest_sha256", "aggregation_signature", "source_checkpoint_sha256")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"Cannot read JSON {path}: {exc}") from exc


def csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key, "") for key in fields})
    return stream.getvalue().encode("utf-8")


def read_csv(path: Path, fields: list[str]) -> list[dict]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != fields:
                raise ArchiveError(f"CSV schema differs in {path}; expected {fields}")
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise ArchiveError(f"Malformed CSV row in {path}")
            return rows
    except OSError as exc:
        raise ArchiveError(f"Cannot read CSV {path}: {exc}") from exc


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value):
        raise ArchiveError(f"Unsafe or invalid identifier: {value!r}")
    return value


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ArchiveError(f"Path escapes archive: {relative}")
    return path


def numeric(value: Any, label: str, optional: bool = False) -> float | None:
    if value is None or value == "":
        if optional:
            return None
        raise ArchiveError(f"Missing numeric value: {label}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"Invalid number {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise ArchiveError(f"Non-finite number {label}")
    return number


def validate(metadata: dict, metrics: list[dict], comparisons: list[dict]) -> None:
    from .metrics import validate_definitions
    validate_definitions(metadata)
    experiment_id = valid_id(metadata.get("experiment_id", ""))
    if metadata.get("schema_version") != 1:
        raise ArchiveError("Unsupported metadata schema_version (expected 1)")
    if not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
        raise ArchiveError("A nonempty title is required")
    sources = metadata.get("source_refs")
    if not isinstance(sources, list) or not sources or any(not isinstance(s, dict) or not s.get("id") for s in sources):
        raise ArchiveError("title and nonempty source_refs are required")
    if len({s["id"] for s in sources}) != len(sources):
        raise ArchiveError("Duplicate source reference ID")
    domains = metadata.get("domains", [])
    if not isinstance(domains, list) or any(not isinstance(d, str) for d in domains):
        raise ArchiveError("domains must be an array of strings")
    if metadata.get("evidence_validity", "unknown") not in ("unknown", "valid", "invalid", "needs_review"):
        raise ArchiveError("Invalid evidence_validity")
    if metadata.get("claim_status", "current") not in ("current", "superseded", "retracted"):
        raise ArchiveError("Invalid claim_status")
    if metadata.get("canonical_experiment_id"):
        valid_id(metadata["canonical_experiment_id"])
        if metadata["canonical_experiment_id"] == experiment_id:
            raise ArchiveError("Canonical experiment cannot refer to itself")
    if not isinstance(metadata.get("attributes", {}), dict):
        raise ArchiveError("attributes must be an object")
    lifecycle = metadata.get("lifecycle", {})
    if not isinstance(lifecycle, dict) or any(not isinstance(value, str) or not value for value in lifecycle.values()):
        raise ArchiveError("lifecycle must be an object of nonempty status strings")
    completeness = metadata.get("completeness", {})
    if (not isinstance(completeness, dict) or any(key not in ("metric_rows", "comparison_rows", "evaluations")
            or type(value) is not int or value < 0 for key, value in completeness.items())):
        raise ArchiveError("completeness requires declared nonnegative metric_rows, comparison_rows or evaluations counts")
    evaluations = metadata.get("evaluations", {})
    if not isinstance(evaluations, dict):
        raise ArchiveError("evaluations must be an object keyed by evaluation_id")
    seen = set()
    for row in metrics:
        if set(row) - set(METRIC_FIELDS):
            raise ArchiveError(f"Unknown CSV identity columns: {set(row) - set(METRIC_FIELDS)}")
        if row.get("experiment_id") != experiment_id:
            raise ArchiveError("CSV experiment_id differs from metadata")
        evaluation_id = row.get("evaluation_id")
        if evaluation_id not in evaluations:
            raise ArchiveError(f"Unknown evaluation_id {evaluation_id}")
        definition = evaluations[evaluation_id]
        for key in ("variant_id", "domain", "prediction_phase"):
            if str(row.get(key, "")) != str(definition.get(key, "")):
                raise ArchiveError(f"{evaluation_id}: CSV {key} differs from evaluation definition")
        key = (evaluation_id, row.get("scope_type"), row.get("scope_id"), row.get("metric"))
        if key in seen:
            raise ArchiveError(f"Duplicate metric key: {key}")
        seen.add(key)
        if not all(row.get(k) for k in ("scope_type", "scope_id", "metric", "source_ref")):
            raise ArchiveError("Metric requires scope, metric name and source_ref")
        status = row.get("value_status", "")
        if status not in ("valid", "unknown", "invalid", "not_applicable"):
            raise ArchiveError(f"Invalid value_status: {status}")
        numeric(row.get("value"), "value", optional=status != "valid")
        if row.get("n_frames") not in (None, ""):
            frames = numeric(row["n_frames"], "n_frames")
            if frames < 0 or not frames.is_integer():
                raise ArchiveError("n_frames must be a nonnegative integer")
    comparison_keys = set()
    for row in comparisons:
        if set(row) - set(COMPARISON_FIELDS):
            raise ArchiveError("Unknown comparison columns")
        if row.get("experiment_id") != experiment_id:
            raise ArchiveError("Comparison experiment_id differs")
        for key in ("candidate_evaluation_id", "baseline_evaluation_id"):
            if row.get(key) not in evaluations:
                raise ArchiveError(f"Comparison references unknown evaluation: {row.get(key)}")
        identity = tuple(row.get(k) for k in ("candidate_evaluation_id", "baseline_evaluation_id", "scope_type", "scope_id", "metric"))
        if identity in comparison_keys:
            raise ArchiveError("Duplicate comparison key")
        comparison_keys.add(identity)
        for key in ("delta", "relative_percent", "percentage_points", "ci_low", "ci_high"):
            numeric(row.get(key), key, optional=key != "delta")
        low, high = numeric(row.get("ci_low"), "ci_low", True), numeric(row.get("ci_high"), "ci_high", True)
        if (low is None) != (high is None) or (low is not None and low > high):
            raise ArchiveError("Invalid CI endpoints")


class Archive:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def init(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.writer():
            if not (self.root / "manifest.json").exists():
                atomic_write(self.root / "manifest.json", canonical({"schema_version": 1, "generation": 0, "experiments": {}}))
            schema = {"schema_version": 1, "metrics_fields": METRIC_FIELDS,
                      "comparison_fields": COMPARISON_FIELDS,
                      "metric_primary_key": ["evaluation_id", "scope_type", "scope_id", "metric"],
                      "missing": "Empty number + value_status, never zero-fill", "encoding": "UTF-8",
                      "attributes": "Open JSON object; absent properties are unknown"}
            if not (self.root / "schema.json").exists():
                atomic_write(self.root / "schema.json", canonical(schema))
        return self.manifest()

    @contextmanager
    def writer(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".write.lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ArchiveError(f"Archive is locked: {path}. Check the recorded PID before recovering a stale lock.") from exc
        try:
            os.write(descriptor, canonical({"pid": os.getpid(), "created_at": now()}))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)

    def manifest(self) -> dict:
        result = read_json(self.root / "manifest.json")
        if result.get("schema_version") != 1 or not isinstance(result.get("experiments"), dict):
            raise ArchiveError("Unsupported or corrupt archive manifest")
        return result

    def snapshot(self) -> tuple[dict, str]:
        manifest = self.manifest()
        return manifest, digest(canonical(manifest))

    def publish(self, metadata: dict, note: str, metrics: list[dict] | None = None,
                comparisons: list[dict] | None = None, reason: str | None = None) -> dict:
        metrics, comparisons = metrics or [], comparisons or []
        metadata = json.loads(canonical(metadata))
        metadata.setdefault("evidence_validity", "unknown")
        metadata.setdefault("claim_status", "current")
        metadata.setdefault("attributes", {})
        metadata.setdefault("evaluations", {})
        validate(metadata, metrics, comparisons)
        experiment_id = metadata["experiment_id"]
        payload = {"metadata.json": canonical(metadata), "note.md": (note.rstrip() + "\n").encode("utf-8"),
                   "metrics.csv": csv_bytes(metrics, METRIC_FIELDS),
                   "comparisons.csv": csv_bytes(comparisons, COMPARISON_FIELDS)}
        content_sha = digest(canonical({name: digest(data) for name, data in payload.items()}))
        if not (self.root / "manifest.json").exists():
            self.init()
        with self.writer():
            manifest = self.manifest()
            previous = manifest["experiments"].get(experiment_id)
            if previous and previous["content_sha256"] == content_sha:
                return {"experiment_id": experiment_id, "revision": previous["revision"], "changed": False}
            if previous and not reason:
                raise ArchiveError("Existing experiment changed; supply an explicit revision reason")
            revision = now().replace(":", "").replace("+", "_") + "_" + content_sha[:12]
            relative = f"experiments/{experiment_id}/revisions/{revision}"
            destination = inside(self.root, relative)
            staging = self.root / ".staging" / uuid.uuid4().hex
            staging.mkdir(parents=True)
            try:
                for name, data in payload.items():
                    atomic_write(staging / name, data)
                receipt = {"revision": revision, "reason": reason or "initial import", "created_at": now(),
                           "previous_revision": previous["revision"] if previous else None,
                           "content_sha256": content_sha,
                           "files": {name: digest(data) for name, data in payload.items()}}
                atomic_write(staging / "receipt.json", canonical(receipt))
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, destination)
                manifest["experiments"][experiment_id] = {"revision": revision, "path": relative,
                    "content_sha256": content_sha, "files": receipt["files"]}
                manifest["generation"] += 1
                atomic_write(self.root / "manifest.json", canonical(manifest))
            finally:
                if staging.exists():
                    if not staging.resolve().is_relative_to(self.root):
                        raise ArchiveError("Refusing staging cleanup outside archive root")
                    shutil.rmtree(staging)
        return {"experiment_id": experiment_id, "revision": revision, "changed": True}

    def record(self, experiment_id: str, manifest: dict | None = None, revision: str | None = None,
               tables: bool = True) -> dict:
        valid_id(experiment_id)
        manifest = manifest if manifest is not None else self.manifest()
        entry = manifest["experiments"].get(experiment_id)
        if not entry:
            raise ArchiveError(f"Unknown experiment: {experiment_id}")
        if revision:
            valid_id(revision)
            folder = inside(self.root, f"experiments/{experiment_id}/revisions/{revision}")
            receipt = read_json(folder / "receipt.json")
            entry = {"revision": revision, "files": receipt["files"]}
        else:
            folder = inside(self.root, entry["path"])
        for name in FILES:
            if file_sha(folder / name) != entry["files"].get(name):
                raise ArchiveError(f"Archive file changed outside revision publishing: {folder / name}")
        metadata = read_json(folder / "metadata.json")
        metrics = read_csv(folder / "metrics.csv", METRIC_FIELDS) if tables else []
        comparisons = read_csv(folder / "comparisons.csv", COMPARISON_FIELDS) if tables else []
        validate(metadata, metrics, comparisons)
        return {"metadata": metadata, "note": (folder / "note.md").read_text(encoding="utf-8"),
                "metrics": metrics, "comparisons": comparisons, "revision": entry["revision"],
                "folder": str(folder), "content_sha256": entry.get("content_sha256")}

    def records(self, manifest: dict | None = None, tables: bool = True):
        manifest = manifest if manifest is not None else self.manifest()
        for experiment_id in sorted(manifest["experiments"]):
            yield self.record(experiment_id, manifest, tables=tables)

    def history(self, experiment_id: str) -> list[dict]:
        valid_id(experiment_id)
        folder = self.root / "experiments" / experiment_id / "revisions"
        return [read_json(path) for path in sorted(folder.glob("*/receipt.json"))]

    def import_bundle(self, path: Path, reason: str | None = None) -> dict:
        return self.publish(read_json(path / "metadata.json"), (path / "note.md").read_text(encoding="utf-8"),
                            read_csv(path / "metrics.csv", METRIC_FIELDS),
                            read_csv(path / "comparisons.csv", COMPARISON_FIELDS), reason)

    def verify(self) -> dict:
        manifest = self.manifest()
        count = metrics = comparisons = 0
        for record in self.records(manifest):
            count += 1
            metrics += len(record["metrics"])
            comparisons += len(record["comparisons"])
        return {"status": "pass", "generation": manifest["generation"], "experiments": count,
                "metric_rows": metrics, "comparison_rows": comparisons,
                "scope": "local current revision hashes, CSV schemas and references; not remote audit"}


def compare_definitions(candidate: dict, baseline: dict) -> dict:
    missing, differences = [], {}
    for key in COMPATIBILITY_KEYS:
        left, right = candidate.get(key), baseline.get(key)
        if left in (None, "", "unknown") or right in (None, "", "unknown"):
            missing.append(key)
        elif left != right:
            differences[key] = {"candidate": left, "baseline": right}
    return {"status": "incompatible" if differences else "unknown" if missing else "compatible",
            "differences": differences, "missing": missing,
            "changed_factors": {key: {"candidate": candidate.get(key), "baseline": baseline.get(key)}
                                for key in ("prediction_phase", "adaptation_schedule", "update_budget")
                                if candidate.get(key) != baseline.get(key)}}
