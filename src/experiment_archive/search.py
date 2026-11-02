from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections import Counter
from pathlib import Path

import numpy as np

from .store import Archive, ArchiveError, atomic_write, canonical, digest, inside, now, read_json


def terms(text: str) -> list[str]:
    text = text.casefold()
    tokens = re.findall(r"[a-z0-9_./-]+", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens += [run[i:i + 2] for i in range(max(0, len(run) - 1))] if len(run) > 1 else [run]
    return tokens


def bm25(query: str, texts: list[str], aliases: dict | None = None) -> list[float]:
    expanded = query + " " + " ".join(value for key, value in (aliases or {}).items() if key in query)
    query_terms = set(terms(expanded))
    counters = [Counter(terms(text)) for text in texts]
    avg_length = sum(map(lambda x: sum(x.values()), counters)) / max(len(texts), 1) or 1
    document_frequency = Counter(key for count in counters for key in count)
    scores = []
    for count in counters:
        length = sum(count.values())
        score = 0.0
        for term in query_terms:
            frequency = count.get(term, 0)
            if frequency:
                idf = math.log(1 + (len(texts) - document_frequency[term] + .5) / (document_frequency[term] + .5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * length / avg_length))
        scores.append(score)
    return scores


def char_chunks(text: str) -> list[str]:
    return [text[start:start + 650] for start in range(0, len(text), 550) if text[start:start + 650].strip()]


def document(record: dict) -> str:
    metadata = record["metadata"]
    search_fields = {key: metadata.get(key) for key in ("title", "kind", "domains", "attributes",
                     "evidence_role", "benchmark_scope", "limitations", "conclusion", "search_terms")}
    if "lifecycle" in metadata:
        search_fields["lifecycle"] = metadata["lifecycle"]
    return json.dumps(search_fields, ensure_ascii=False) + "\n\n" + record["note"]


def get_attribute(metadata: dict, path: str):
    value = metadata
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def match_where(metadata: dict, expression: str, include_unknown: bool = False) -> bool:
    match = re.fullmatch(r"([\w.-]+)\s*(>=|<=|!=|=|>|<)\s*(.+)", expression)
    if not match:
        raise ArchiveError(f"Invalid --where expression: {expression}")
    path, operator, raw = match.groups()
    if not path.startswith(("attributes.", "evaluations.", "lifecycle.")) and path not in metadata:
        path = "attributes." + path
    value = get_attribute(metadata, path)
    if value is None:
        return include_unknown
    try:
        expected = json.loads(raw)
    except ValueError:
        expected = raw.strip("\"'")
    try:
        if operator == "=":
            return expected in value if isinstance(value, list) else value == expected
        if operator == "!=":
            return expected not in value if isinstance(value, list) else value != expected
        return {">": lambda: value > expected, "<": lambda: value < expected,
                ">=": lambda: value >= expected, "<=": lambda: value <= expected}[operator]()
    except TypeError:
        return False


class SearchIndex:
    def __init__(self, archive: Archive):
        self.archive = archive
        self.cache = archive.root / ".cache"

    def build(self, embedder, progress=None) -> dict:
        start = time.perf_counter()
        manifest, snapshot_sha = self.archive.snapshot()
        chunks = []
        for record in self.archive.records(manifest, tables=False):
            for number, text in enumerate(embedder.chunks(document(record))):
                chunks.append({"experiment_id": record["metadata"]["experiment_id"], "number": number,
                               "revision": record["revision"], "content_sha256": record["content_sha256"],
                               "text": text, "text_sha256": digest(text.encode("utf-8"))})
        if not chunks:
            raise ArchiveError("Archive has no text to index")
        vector_cache = self.cache / "vectors" / embedder.fingerprint
        vector_cache.mkdir(parents=True, exist_ok=True)
        vectors, missing = {}, {}
        reused = 0
        for chunk in chunks:
            key = chunk["text_sha256"]
            path = vector_cache / (key + ".npy")
            if key in vectors or key in missing:
                continue
            if path.exists():
                try:
                    vector = np.load(path, allow_pickle=False)
                    if vector.shape != (embedder.identity["dimension"],) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-3):
                        raise ValueError("Invalid vector cache")
                    vectors[key] = vector
                    reused += 1
                    continue
                except (OSError, ValueError, EOFError):
                    pass
            missing[key] = chunk["text"]
        items = list(missing.items())
        for offset in range(0, len(items), 16):
            batch = items[offset:offset + 16]
            output = embedder.encode([text for _, text in batch])
            for (key, _), vector in zip(batch, output, strict=True):
                vectors[key] = vector
                import io
                stream = io.BytesIO()
                np.save(stream, vector, allow_pickle=False)
                atomic_write(vector_cache / (key + ".npy"), stream.getvalue())
            if progress:
                progress(min(offset + len(batch), len(items)), len(items))
        matrix = np.stack([vectors[c["text_sha256"]] for c in chunks]).astype(np.float32)
        generation = uuid.uuid4().hex
        folder = self.cache / "indexes" / generation
        folder.mkdir(parents=True)
        np.save(folder / "embeddings.npy", matrix, allow_pickle=False)
        atomic_write(folder / "chunks.json", canonical(chunks))
        report = {"created_at": now(), "archive_snapshot_sha256": snapshot_sha,
                  "archive_generation": manifest["generation"], "model": embedder.identity,
                  "chunks": len(chunks), "unique_texts": len(vectors), "encoded": len(items), "reused": reused,
                  "vector_bytes": matrix.nbytes, "seconds": time.perf_counter() - start,
                  "chunking": "token-offset-window-440-overlap-60-v1"}
        from .store import file_sha
        report["files"] = {name: file_sha(folder / name) for name in ("embeddings.npy", "chunks.json")}
        atomic_write(folder / "manifest.json", canonical(report))
        # Publish only if no concurrent archive revision changed the indexed snapshot.
        with self.archive.writer():
            if self.archive.snapshot()[1] != snapshot_sha:
                raise ArchiveError("Archive changed while indexing; rerun index (vectors are cached)")
            atomic_write(self.cache / "index.json", canonical({"path": f".cache/indexes/{generation}"}))
        return report

    def status(self) -> dict:
        pointer = self.cache / "index.json"
        if not pointer.exists():
            return {"exists": False, "stale": True}
        folder = inside(self.archive.root, read_json(pointer)["path"])
        report = read_json(folder / "manifest.json")
        return {"exists": True, "stale": report["archive_snapshot_sha256"] != self.archive.snapshot()[1], **report}

    def search(self, query: str, embedder=None, mode: str = "hybrid", limit: int = 5,
               domain: str | None = None, include_retracted: bool = False,
               validity: str | None = None, where: list[str] | None = None,
               include_unknown: bool = False, max_chars: int = 1200, offset: int = 0,
               sort: str = "relevance") -> dict:
        from .presentation import record_time, sort_by_time, lifecycle
        if sort not in ("relevance", "time_asc", "time_desc"):
            raise ArchiveError("Unknown sort order")
        start = time.perf_counter()
        if not query.strip():
            raise ArchiveError("Query cannot be empty")
        if not 1 <= limit <= 50 or max_chars < 100 or offset < 0:
            raise ArchiveError("limit must be 1..50, max_chars >=100, offset >=0")
        manifest, snapshot_sha = self.archive.snapshot()
        records = {r["metadata"]["experiment_id"]: r for r in self.archive.records(manifest, tables=False)}
        canonical_ids = {}
        for experiment_id, record in records.items():
            target = record["metadata"].get("canonical_experiment_id", experiment_id)
            if target not in records:
                raise ArchiveError(f"Canonical experiment is missing: {target}")
            if target != experiment_id and records[target]["metadata"].get("canonical_experiment_id"):
                raise ArchiveError("Canonical links must point directly to a primary experiment (no chains/cycles)")
            canonical_ids[experiment_id] = target
        eligible, excluded = set(), {"retracted_or_invalid": 0, "filters": 0}
        for experiment_id, record in records.items():
            metadata = records[canonical_ids[experiment_id]]["metadata"]
            if not include_retracted and (metadata.get("claim_status") in ("retracted", "superseded") or metadata.get("evidence_validity") == "invalid"):
                excluded["retracted_or_invalid"] += 1
                continue
            if (domain and domain.casefold() not in [x.casefold() for x in metadata.get("domains", [])]) or (validity and metadata.get("evidence_validity") != validity) or not all(match_where(metadata, expr, include_unknown) for expr in (where or [])):
                excluded["filters"] += 1
                continue
            eligible.add(experiment_id)
        if mode == "lexical":
            chunks = [{"experiment_id": eid, "text": text, "number": number}
                      for eid, record in records.items() if eid in eligible
                      for number, text in enumerate(char_chunks(document(record))) ]
            similarity = None
        else:
            if embedder is None:
                raise ArchiveError("Semantic/hybrid search requires a local embedding model")
            pointer = self.cache / "index.json"
            if not pointer.exists():
                raise ArchiveError("No index. Run index, or use search --mode lexical")
            folder = inside(self.archive.root, read_json(pointer)["path"])
            report = read_json(folder / "manifest.json")
            if report["archive_snapshot_sha256"] != snapshot_sha:
                raise ArchiveError("Index is stale. Run index, or use search --mode lexical; old and new evidence are not combined")
            if report["model"]["fingerprint"] != embedder.fingerprint:
                raise ArchiveError("Embedding model differs from index; rebuild index")
            from .store import file_sha
            for name, expected in report["files"].items():
                if file_sha(folder / name) != expected:
                    raise ArchiveError(f"Corrupt index file: {name}; rebuild index")
            all_chunks = read_json(folder / "chunks.json")
            matrix = np.load(folder / "embeddings.npy", allow_pickle=False)
            if matrix.shape != (len(all_chunks), embedder.identity["dimension"]):
                raise ArchiveError("Index dimensions differ")
            indices = [i for i, chunk in enumerate(all_chunks) if chunk["experiment_id"] in eligible]
            chunks = [all_chunks[i] for i in indices]
            similarity = matrix[indices] @ embedder.encode([query], query=True)[0] if indices else np.array([])
        if not chunks:
            return {"query": query, "mode": mode, "total": 0, "results": [], "excluded": excluded,
                    "archive_generation": manifest["generation"]}
        aliases = {}
        for identity in eligible:
            for key, value in records[identity]["metadata"].get("search_aliases", {}).items():
                # Union project vocabularies without silently overwriting a homonym.
                aliases[key] = " ".join(sorted(set((aliases.get(key, "") + " " + value).split())))
        lexical = bm25(query, [c["text"] for c in chunks], aliases)
        ranks = {}
        if mode != "semantic":
            for rank, index in enumerate(sorted((i for i, value in enumerate(lexical) if value > 0), key=lambda i: lexical[i], reverse=True), 1):
                ranks[index] = ranks.get(index, 0) + 1 / (60 + rank)
        if similarity is not None:
            for rank, index in enumerate(np.argsort(-similarity).tolist(), 1):
                ranks[index] = ranks.get(index, 0) + 1 / (60 + rank)
        best = {}
        for index in sorted(ranks, key=lambda i: (-ranks[i], chunks[i]["experiment_id"], chunks[i]["number"])):
            chunk = chunks[index]
            canonical_id = canonical_ids[chunk["experiment_id"]]
            if canonical_id not in best:
                best[canonical_id] = (index, chunk)
        results = []
        for experiment_id, (index, chunk) in best.items():
            record = records[experiment_id]
            metadata = record["metadata"]
            matched_record = records[chunk["experiment_id"]]
            results.append({"experiment_id": experiment_id, "title": metadata["title"],
                **record_time(metadata, record["revision"].split("_")[0]), "lifecycle": lifecycle(metadata),
                "benchmark_scope": metadata.get("benchmark_scope", "unknown"),
                "limitations": metadata.get("limitations", []),
                "kind": metadata.get("kind", "unknown"),
                "score": ranks[index], "semantic_similarity": float(similarity[index]) if similarity is not None else None,
                "evidence_validity": metadata.get("evidence_validity"), "claim_status": metadata.get("claim_status"),
                "audit_status": metadata.get("audit_status", "unknown"),
                "verification_basis": metadata.get("verification_basis", "unknown"),
                "evidence_role": metadata.get("evidence_role", "unknown"), "domains": metadata.get("domains", []),
                "revision": record["revision"], "content_sha256": record["content_sha256"],
                "snippet": chunk["text"][:max_chars], "snippet_truncated": len(chunk["text"]) > max_chars,
                "matched_record_id": chunk["experiment_id"], "matched_revision": matched_record["revision"],
                "matched_evidence_validity": matched_record["metadata"].get("evidence_validity"),
                "matched_note_path": str(Path(matched_record["folder"]) / "note.md"),
                "note_path": str(Path(record["folder"]) / "note.md"), "source_refs": metadata["source_refs"]})
        if sort != "relevance":
            results = sort_by_time(results, sort, "experiment_id")
        results = results[offset:offset + limit]
        for hit in results:
            record = self.archive.record(hit["experiment_id"], manifest)
            evaluations = record["metadata"].get("evaluations", {})
            metric_lookup = {(row["evaluation_id"], row["scope_type"], row["scope_id"], row["metric"]): row
                             for row in record["metrics"]}
            rows = record["comparisons"] or record["metrics"]
            rows = [row for row in rows if not domain or row.get("domain", "").casefold() == domain.casefold()]
            rows.sort(key=lambda row: not (row.get("domain") and row["domain"].casefold() in query.casefold()))
            contexts, seen = [], set()
            for row in rows:
                candidate = row.get("candidate_evaluation_id", row.get("evaluation_id"))
                baseline = row.get("baseline_evaluation_id")
                key = (candidate, baseline, row.get("scope_type"), row.get("scope_id"))
                if key in seen:
                    continue
                seen.add(key)
                metric_row = metric_lookup.get((candidate, row.get("scope_type"), row.get("scope_id"), row.get("metric")), row)
                contexts.append({**{key: row.get(key) for key in ("domain", "scope_type", "scope_id", "n_frames", "prediction_phase")},
                    "n_frames": metric_row.get("n_frames"), "compatibility": row.get("compatibility", "unknown"),
                    "candidate": evaluations.get(candidate, {}), "baseline": evaluations.get(baseline, {})})
                if len(contexts) == 4:
                    break
            hit["comparison_context"] = contexts
        return {"query": query, "mode": mode, "sort": sort, "archive_generation": manifest["generation"],
                "total": len(best), "offset": offset, "next_offset": offset + limit if offset + limit < len(best) else None,
                "excluded": excluded, "results": results, "seconds": time.perf_counter() - start,
                "notice": "Scores measure relevance; order follows requested sort. Read matched evaluation and CSV for benefit and validity."}
