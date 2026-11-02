from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .importers import import_markdown
from .adapters import ingest_source, list_adapters
from .metrics import normalize_metric, percentage_points
from .search import SearchIndex
from .store import (Archive, ArchiveError, COMPARISON_FIELDS, METRIC_FIELDS,
                    atomic_write, canonical, compare_definitions, csv_bytes, inside, valid_id)


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="全本地实验归档：Markdown/JSON/CSV + multilingual E5 ONNX检索")
    result.add_argument("--archive", type=Path, default=Path.cwd() / "archive", help="归档目录，默认当前目录/archive")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化归档，不覆盖已有文件")
    sub.add_parser("status", help="归档与向量索引状态")
    sub.add_parser("verify", help="验证本地文件哈希、CSV schema/主键与评价引用")
    sub.add_parser("accept", help="统一验收：来源、评价、基线、身份、完整性与索引回执")
    sync = sub.add_parser("sync", help="脚本化增量导入→校验→更新索引，源文件只读")
    sync.add_argument("--config", type=Path, default=Path.cwd() / "pipeline.json")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--index-mode", choices=["semantic", "none"], default="semantic")
    sync.add_argument("--threads", type=int, default=2)
    sync.add_argument("--batch-size", type=int, default=4)
    evidence = sub.add_parser("evidence", help="一次检索并读取有界的原文、指标、比较和评价上下文")
    evidence.add_argument("query")
    evidence.add_argument("--domain")
    evidence.add_argument("--metric", help="指标名或当前记录声明的别名；省略时读取有界的全部指标")
    evidence.add_argument("--scope-type", default="domain", help="domain / sequence / paper_split / all 等")
    evidence.add_argument("--mode", choices=["hybrid", "semantic", "lexical"], default="hybrid")
    evidence.add_argument("--sort", choices=["relevance", "time_desc", "time_asc"], default="relevance")
    evidence.add_argument("--limit", type=int, default=3)
    evidence.add_argument("--rows-per-record", type=int, default=8)
    evidence.add_argument("--include-retracted", action="store_true")
    evidence.add_argument("--no-refresh-index", action="store_true")
    evidence.add_argument("--threads", type=int, default=2)
    evidence.add_argument("--batch-size", type=int, default=4)
    evidence.add_argument("--language", choices=["zh-CN", "en"], help="显式生成派生译文；保留原始指标与证据")
    evidence.add_argument("--translation-provider", choices=["local", "google", "ollama"], help="google会发送命中片段文本到公网")
    translate = sub.add_parser("translate", help="翻译单条记录；原始文件不变，译文独立缓存")
    translate.add_argument("experiment_id")
    translate.add_argument("--language", choices=["zh-CN", "en"], default="zh-CN")
    translate.add_argument("--provider", choices=["local", "google", "ollama"])
    translate.add_argument("--revision")
    translation_settings = sub.add_parser("translation-settings", help="读取或保存翻译后端配置，不发送原文")
    translation_settings.add_argument("--provider", choices=["local", "google", "ollama"])
    translation_settings.add_argument("--ollama-url")
    translation_settings.add_argument("--ollama-model")
    translation_model = sub.add_parser("translation-model", help="显式下载安装本地中英专用模型（约145MB压缩包）")
    translation_model.add_argument("action", choices=["download"])
    new = sub.add_parser("new", help="创建可编辑草稿；发布前不影响检索")
    new.add_argument("experiment_id")
    new.add_argument("--title", required=True)
    publish = sub.add_parser("publish", help="发布包含四个文件的bundle；变更必须给出reason")
    publish.add_argument("bundle", type=Path)
    publish.add_argument("--reason")
    imp = sub.add_parser("import-markdown", help="只读导入原文，不自动确认科学结论或数值")
    imp.add_argument("path", type=Path)
    imp.add_argument("--split-sections", action="store_true")
    imp.add_argument("--id")
    imp.add_argument("--reason")
    sub.add_parser("adapters", help="列出通用来源和已安装的可选项目适配器")
    ingest = sub.add_parser("ingest", help="通过来源接口导入项目；通用核心不解析项目格式")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--adapter", required=True)
    ingest.add_argument("--options", default="{}", help="适配器选项JSON对象")
    ingest.add_argument("--reason", default="Explicit adapter import")
    show = sub.add_parser("show", help="读取单个实验的有界卡片、元数据或文件位置")
    show.add_argument("experiment_id")
    show.add_argument("--view", choices=["card", "metadata", "files"], default="card")
    show.add_argument("--revision")
    show.add_argument("--max-chars", type=int, default=4500)
    history = sub.add_parser("history", help="查看修订原因、旧版本与文件哈希")
    history.add_argument("experiment_id")
    checkout = sub.add_parser("checkout", help="复制已发布记录到可编辑草稿")
    checkout.add_argument("experiment_id")
    revise = sub.add_parser("revise", help="追加元数据修订，如撤回、字段扩展；不覆盖历史")
    revise.add_argument("experiment_id")
    revise.add_argument("--set", action="append", required=True, dest="changes")
    revise.add_argument("--reason", required=True)
    for name in ("metrics", "comparisons"):
        table = sub.add_parser(name, help="按需读取本地CSV，默认有界输出")
        table.add_argument("experiment_id")
        table.add_argument("--domain")
        table.add_argument("--scope-type")
        table.add_argument("--scope-id")
        table.add_argument("--metric")
        table.add_argument("--evaluation-id")
        table.add_argument("--variant")
        table.add_argument("--limit", type=int, default=20)
        table.add_argument("--offset", type=int, default=0)
        table.add_argument("--export", type=Path, help="明确导出全部筛选结果，不把全表塞进对话")
    compare = sub.add_parser("compare", help="核验两个evaluation可比性，仅通过时计算新差值")
    compare.add_argument("candidate_evaluation_id")
    compare.add_argument("baseline_evaluation_id")
    compare.add_argument("--metric", required=True)
    model = sub.add_parser("model", help="显式下载/核验固定版本量化模型")
    model.add_argument("action", choices=["download"])
    for command in ("index", "search"):
        part = sub.add_parser(command, help="增量构建本地索引" if command == "index" else "本地语义/关键词/混合检索")
        part.add_argument("--threads", type=int, default=2)
        part.add_argument("--batch-size", type=int, default=4)
        if command == "search":
            part.add_argument("query")
            part.add_argument("--mode", choices=["hybrid", "semantic", "lexical"], default="hybrid")
            part.add_argument("--sort", choices=["relevance", "time_desc", "time_asc"], default="relevance")
            part.add_argument("--domain")
            part.add_argument("--validity", choices=["valid", "unknown", "invalid", "needs_review"])
            part.add_argument("--include-retracted", action="store_true")
            part.add_argument("--where", action="append")
            part.add_argument("--include-unknown", action="store_true")
            part.add_argument("--limit", type=int, default=5)
            part.add_argument("--offset", type=int, default=0)
            part.add_argument("--max-chars", type=int, default=700)
    return result


def model_path(archive: Archive) -> Path:
    return archive.root / ".cache" / "models" / "multilingual-e5-small"


def write_draft(archive: Archive, experiment_id: str, metadata: dict, note: str,
                metrics: list[dict], comparisons: list[dict]) -> Path:
    valid_id(experiment_id)
    folder = inside(archive.root, f"drafts/{experiment_id}")
    if folder.exists():
        raise ArchiveError(f"Draft already exists: {folder}")
    folder.mkdir(parents=True)
    atomic_write(folder / "metadata.json", canonical(metadata))
    atomic_write(folder / "note.md", note.encode("utf-8"))
    atomic_write(folder / "metrics.csv", csv_bytes(metrics, METRIC_FIELDS))
    atomic_write(folder / "comparisons.csv", csv_bytes(comparisons, COMPARISON_FIELDS))
    return folder


def set_value(metadata: dict, expression: str) -> None:
    if "=" not in expression:
        raise ArchiveError("--set must be dotted.path=JSON_value")
    path, raw = expression.split("=", 1)
    keys = path.split(".")
    if not all(keys) or keys[0] in ("experiment_id", "schema_version"):
        raise ArchiveError("Cannot change identity/schema through revise")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    parent = metadata
    for key in keys[:-1]:
        if key not in parent:
            parent[key] = {}
        if not isinstance(parent[key], dict):
            raise ArchiveError(f"Cannot traverse non-object field {key}")
        parent = parent[key]
    parent[keys[-1]] = value


def run(args) -> dict:
    archive = Archive(args.archive)
    command = args.command
    if command == "sync":
        from .pipeline import sync
        return sync(archive, args.config, threads=args.threads, batch_size=args.batch_size,
                    dry_run=args.dry_run, index_mode=args.index_mode)
    if command == "evidence":
        from .pipeline import evidence
        result = evidence(archive, args.query, mode=args.mode, domain=args.domain, metric=args.metric,
                        scope_type=args.scope_type, limit=args.limit, rows_per_record=args.rows_per_record,
                        include_retracted=args.include_retracted, threads=args.threads, batch_size=args.batch_size,
                        refresh_index=not args.no_refresh_index, sort=args.sort)
        if args.language:
            from .translation import TranslationService
            result = TranslationService(archive).evidence(result, args.language, args.translation_provider)
        return result
    if command in ("translate", "translation-settings"):
        from .translation import TranslationService
        service = TranslationService(archive)
        if command == "translate":
            return service.record(args.experiment_id, args.language, args.provider, args.revision)
        patch = {key: getattr(args, key) for key in ("provider", "ollama_url", "ollama_model") if getattr(args, key)}
        return service.save_settings(patch) if patch else service.settings()
    if command == "translation-model":
        from .translation import install_local_models
        return install_local_models(archive)
    if command == "init":
        return {"archive": str(archive.root), **archive.init()}
    if command == "model":
        from .embedding import download_model
        report = download_model(model_path(archive))
        atomic_write(model_path(archive) / "download_receipt.json", canonical(report))
        return report
    if command == "adapters":
        return {"source_adapter_api_version": 1, "adapters": list_adapters()}
    if command == "ingest":
        options = json.loads(args.options)
        if not isinstance(options, dict):
            raise ArchiveError("--options must be a JSON object")
        return ingest_source(archive, args.adapter, args.path, options, args.reason)
    if command == "import-markdown":
        return import_markdown(archive, args.path, args.split_sections, args.id, args.reason)
    if command == "publish":
        return archive.import_bundle(args.bundle, args.reason)
    if command == "verify":
        return archive.verify()
    if command == "accept":
        from .acceptance import accept
        return accept(archive)
    if command == "status":
        manifest = archive.manifest()
        return {"archive": str(archive.root), "experiments": len(manifest["experiments"]),
                "generation": manifest["generation"], "index": SearchIndex(archive).status()}
    if command == "new":
        metadata = {"schema_version": 1, "experiment_id": args.experiment_id, "title": args.title,
            "kind": "experiment", "domains": [], "attributes": {}, "evaluations": {},
            "evidence_validity": "unknown", "claim_status": "current", "audit_status": "unknown",
            "verification_basis": "manual_draft", "source_refs": [{"id": "source", "description": "填写实际证据路径/哈希/来源"}]}
        note = f"# {args.title}\n\n## 问题与方法\n\n## 结果\n\n## 局限\n\n## 证据来源\n"
        folder = write_draft(archive, args.experiment_id, metadata, note, [], [])
        return {"draft": str(folder), "notice": "Edit four files, then publish; draft is not indexed."}
    if command == "history":
        return {"experiment_id": args.experiment_id, "history": archive.history(args.experiment_id)}
    if command in ("show", "checkout", "revise", "metrics", "comparisons"):
        record = archive.record(args.experiment_id, revision=getattr(args, "revision", None))
        if command == "checkout":
            folder = write_draft(archive, args.experiment_id, record["metadata"], record["note"], record["metrics"], record["comparisons"])
            return {"draft": str(folder), "based_on_revision": record["revision"]}
        if command == "revise":
            for change in args.changes:
                set_value(record["metadata"], change)
            return archive.publish(record["metadata"], record["note"], record["metrics"], record["comparisons"], args.reason)
        if command == "show":
            if args.view == "metadata":
                return {"revision": record["revision"], "metadata": record["metadata"]}
            if args.view == "files":
                return {"revision": record["revision"], "folder": record["folder"],
                        "files": {name: str(Path(record["folder"]) / name) for name in ("note.md", "metadata.json", "metrics.csv", "comparisons.csv")}}
            return {"experiment_id": args.experiment_id, "title": record["metadata"]["title"],
                    "revision": record["revision"], "evidence_validity": record["metadata"].get("evidence_validity"),
                    "verification_basis": record["metadata"].get("verification_basis"),
                    "note": record["note"][:args.max_chars], "truncated": len(record["note"]) > args.max_chars,
                    "note_path": str(Path(record["folder"]) / "note.md"), "source_refs": record["metadata"]["source_refs"]}
        if not 1 <= args.limit <= 1000 or args.offset < 0:
            raise ArchiveError("limit must be 1..1000 and offset nonnegative")
        metric = normalize_metric(args.metric, record["metadata"])
        rows = [row for row in record[command] if
                (not args.domain or row["domain"] == args.domain) and
                (not args.scope_type or row["scope_type"] == args.scope_type) and
                (not args.scope_id or row["scope_id"] == args.scope_id) and
                (not metric or row["metric"] == metric) and
                (not args.evaluation_id or row.get("evaluation_id", row.get("candidate_evaluation_id")) == args.evaluation_id) and
                (not args.variant or (row["variant_id"] if "variant_id" in row else record["metadata"]["evaluations"][row["candidate_evaluation_id"]]["variant_id"]) == args.variant)]
        if args.export:
            target = args.export.resolve()
            if target.is_relative_to(archive.root) or target.exists():
                raise ArchiveError("Export must use a new file outside the archive; published evidence cannot be overwritten")
            atomic_write(args.export, csv_bytes(rows, METRIC_FIELDS if command == "metrics" else COMPARISON_FIELDS))
            return {"export": str(args.export.resolve()), "rows": len(rows), "revision": record["revision"]}
        return {"experiment_id": args.experiment_id, "revision": record["revision"], "total": len(rows),
                "rows": rows[args.offset:args.offset + args.limit],
                "next_offset": args.offset + args.limit if args.offset + args.limit < len(rows) else None}
    if command == "compare":
        located = {}
        for record in archive.records():
            for eid in set((args.candidate_evaluation_id, args.baseline_evaluation_id)):
                if eid in record["metadata"]["evaluations"]:
                    if eid in located:
                        raise ArchiveError(f"Ambiguous evaluation ID: {eid}")
                    located[eid] = record
        if len(located) != len(set((args.candidate_evaluation_id, args.baseline_evaluation_id))):
            raise ArchiveError("Evaluation not found")
        candidate = located[args.candidate_evaluation_id]
        baseline = located[args.baseline_evaluation_id]
        candidate_metric = normalize_metric(args.metric, candidate["metadata"])
        baseline_metric = normalize_metric(args.metric, baseline["metadata"])
        if candidate_metric != baseline_metric:
            raise ArchiveError("Metric alias resolves differently in candidate and baseline")
        metric = candidate_metric
        result = compare_definitions(candidate["metadata"]["evaluations"][args.candidate_evaluation_id],
                                     baseline["metadata"]["evaluations"][args.baseline_evaluation_id])
        result["candidate_evaluation_id"], result["baseline_evaluation_id"] = args.candidate_evaluation_id, args.baseline_evaluation_id
        result["computed_deltas"] = []
        evidence_states = {r["metadata"]["experiment_id"]: {"validity": r["metadata"].get("evidence_validity", "unknown"),
                            "claim_status": r["metadata"].get("claim_status", "current")} for r in (candidate, baseline)}
        result["evidence_states"] = evidence_states
        if any(s["validity"] == "invalid" or s["claim_status"] != "current" for s in evidence_states.values()):
            result["status"] = "invalid_evidence"
        elif any(s["validity"] != "valid" for s in evidence_states.values()):
            result["status"] = "unknown"
        if result["status"] == "compatible":
            base_rows = {(r["scope_type"], r["scope_id"]): r for r in baseline["metrics"]
                         if r["evaluation_id"] == args.baseline_evaluation_id and r["metric"] == metric and r["value_status"] == "valid"}
            for row in candidate["metrics"]:
                base = base_rows.get((row["scope_type"], row["scope_id"]))
                if row["evaluation_id"] == args.candidate_evaluation_id and row["metric"] == metric and row["value_status"] == "valid" and base:
                    delta = float(row["value"]) - float(base["value"])
                    result["computed_deltas"].append({"scope_id": row["scope_id"], "metric": metric,
                        "delta": delta, "relative_percent": delta / float(base["value"]) * 100 if float(base["value"]) else None,
                        "percentage_points": 100 * delta if percentage_points(metric, candidate["metadata"]) else None})
        return result
    if command in ("index", "search"):
        embedder = None
        if command == "index" or args.mode != "lexical":
            from .embedding import E5Embedder
            embedder = E5Embedder(model_path(archive), args.threads, args.batch_size)
        index = SearchIndex(archive)
        if command == "index":
            return index.build(embedder, lambda done, total: print(f"embedding {done}/{total}", file=sys.stderr, flush=True))
        return index.search(args.query, embedder, args.mode, args.limit, args.domain,
                            args.include_retracted, args.validity, args.where, args.include_unknown,
                            args.max_chars, args.offset, sort=args.sort)
    raise ArchiveError(f"Unsupported command {command}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = run(args)
        emit(result)
        if args.command == "accept":
            return {"pass": 0, "failed": 1, "needs_review": 3}.get(result.get("status"), 2)
        return 0
    except (ArchiveError, OSError, ValueError) as exc:
        emit({"error": str(exc), "command": args.command})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
