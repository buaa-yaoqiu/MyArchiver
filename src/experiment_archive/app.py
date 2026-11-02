"""Local desktop/web UI over the generic archive API; no web framework required."""
from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import list_adapters
from .api import ArchiveClient
from .metrics import normalize_metric
from .pipeline import direction, load_config, local_embedder
from .search import SearchIndex
from .store import ArchiveError, atomic_write, canonical, csv_bytes, now
from .translation import TranslationService, language
from .presentation import record_time, sort_by_time, lifecycle


class AppService:
    def __init__(self, archive: Path, config: Path | None = None, embedder_factory=None):
        self.settings_path = archive.resolve() / ".cache/app-settings.json"
        from .store import read_json
        saved = read_json(self.settings_path) if self.settings_path.is_file() else {}
        archive = Path(saved.get("archive_path", archive))
        config = Path(saved["config_path"]) if saved.get("config_path") else config
        self.client = ArchiveClient(archive)
        self.client.archive.init()
        self.config = config.resolve() if config else None
        self.token = secrets.token_urlsafe(32)
        self.model = None
        self.model_factory = embedder_factory or (lambda: local_embedder(self.client.archive))
        self.model_lock = threading.RLock()
        self.jobs_lock = threading.Lock()
        self.request_lock = threading.RLock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-task")
        self.catalog_lock = threading.Lock()
        self.catalog_sha = None
        self.catalog_cache = None
        self.translator = TranslationService(self.client.archive)

    def close(self):
        self.executor.shutdown(wait=True)

    def catalog(self) -> dict:
        archive = self.client.archive
        manifest, snapshot = archive.snapshot()
        with self.catalog_lock:
            if self.catalog_sha == snapshot:
                return self.catalog_cache
            records = {r["metadata"]["experiment_id"]: r for r in archive.records(manifest)}
            cards, metrics, comparisons = [], 0, 0
            for identity, record in records.items():
                metadata = record["metadata"]
                metrics += len(record["metrics"])
                comparisons += len(record["comparisons"])
                target = metadata.get("canonical_experiment_id")
                if target and target in records:
                    continue
                cards.append({"id": identity, "title": metadata["title"], "kind": metadata.get("kind", "experiment"),
                    **record_time(metadata, record["revision"].split("_")[0]), "lifecycle": lifecycle(metadata),
                    "domains": metadata.get("domains", []), "validity": metadata.get("evidence_validity", "unknown"),
                    "claim_status": metadata.get("claim_status", "current"),
                    "verification_basis": metadata.get("verification_basis", "unknown"),
                    "evidence_role": metadata.get("evidence_role", "unknown"), "revision": record["revision"],
                    "metric_count": len(record["metrics"]), "comparison_count": len(record["comparisons"]),
                    "excerpt": excerpt(record["note"]), "has_metrics": bool(record["metrics"])})
            cards.sort(key=lambda card: (not card["has_metrics"], card["title"]), reverse=False)
            result = {"generation": manifest["generation"], "snapshot": snapshot, "cards": cards,
                "record_count": len(records), "experiment_count": len(cards), "metric_count": metrics,
                "comparison_count": comparisons,
                "domains": sorted({d for card in cards for d in card["domains"]}),
                "kinds": sorted({card["kind"] for card in cards})}
            self.catalog_cache, self.catalog_sha = result, snapshot
            return result

    def status(self) -> dict:
        catalog = self.catalog()
        index = SearchIndex(self.client.archive).status()
        return {key: catalog[key] for key in ("generation", "record_count", "experiment_count", "metric_count", "comparison_count", "domains", "kinds")} | {
            "index": index, "model_loaded": self.model is not None, "archive_path": str(self.client.archive.root),
            "config_path": str(self.config) if self.config else None,
            "sync_available": bool(self.config and self.config.is_file()), "adapters": list_adapters(),
            "translation": self.translator.settings()}

    def experiments(self, query: dict) -> dict:
        catalog = self.catalog()
        offset, limit = pagination(query)
        text, domain, validity = first(query, "q", "").casefold(), first(query, "domain"), first(query, "validity")
        structured = first(query, "structured") == "true"
        cards = [card for card in catalog["cards"] if
            (not text or text in (card["title"] + " " + card["excerpt"] + " " + card["id"]).casefold())
            and (not domain or domain in card["domains"])
            and (not validity or card["validity"] == validity)
            and (not structured or card["has_metrics"])]
        order = first(query, "sort", "time_desc")
        if order not in ("time_desc", "time_asc"):
            raise ArchiveError("Unknown sort order")
        cards = sort_by_time(cards, order, "id")
        return {"total": len(cards), "offset": offset, "limit": limit, "items": cards[offset:offset + limit]}

    def detail(self, identity: str, revision: str | None = None) -> dict:
        record = self.client.read(identity, revision=revision)
        metadata = record["metadata"]
        return {"id": identity, "revision": record["revision"], "metadata": metadata, "note": record["note"],
            "folder": record["folder"], "metric_count": len(record["metrics"]), "comparison_count": len(record["comparisons"]),
            "filters": {key: sorted({row[key] for row in record["metrics"] + record["comparisons"] if row.get(key)})
                        for key in ("domain", "scope_type", "scope_id", "metric")},
            "variants": sorted({row["variant_id"] for row in record["metrics"]}),
            "history": self.client.archive.history(identity)}

    def table(self, query: dict, export: bool = False) -> dict:
        identity = required(query, "id")
        kind = first(query, "kind", "metrics")
        if kind not in ("metrics", "comparisons"):
            raise ArchiveError("Unknown table")
        record = self.client.read(identity, revision=first(query, "revision"))
        metadata = record["metadata"]
        metric = normalize_metric(first(query, "metric"), metadata)
        rows = []
        for original in record[kind]:
            if metric and original["metric"] != metric:
                continue
            if any(first(query, key) and original.get(key) != first(query, key) for key in ("domain", "scope_type", "scope_id")):
                continue
            row = dict(original)
            if kind == "comparisons":
                candidate = metadata["evaluations"][row["candidate_evaluation_id"]]
                baseline = metadata["evaluations"][row["baseline_evaluation_id"]]
                row.update(candidate_variant=candidate["variant_id"], baseline_variant=baseline["variant_id"],
                           prediction_phase=candidate.get("prediction_phase", "unknown"))
                row["numeric_direction"], row["ci_direction"] = direction(row, metadata)
                variant = candidate["variant_id"]
            else:
                variant = row["variant_id"]
            if first(query, "variant") and variant != first(query, "variant"):
                continue
            rows.append(row)
        if export:
            from .store import COMPARISON_FIELDS, METRIC_FIELDS
            return {"data": csv_bytes(rows, METRIC_FIELDS if kind == "metrics" else COMPARISON_FIELDS),
                    "filename": f"{identity}_{kind}.csv"}
        offset, limit = pagination(query)
        visible = rows[offset:offset + limit]
        evaluation_ids = set()
        for row in visible:
            evaluation_ids.update(row[key] for key in ("evaluation_id", "candidate_evaluation_id", "baseline_evaluation_id") if row.get(key))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": visible,
                "revision": record["revision"], "metric_definitions": metadata.get("metric_definitions", {}),
                "evaluations": {eid: metadata["evaluations"][eid] for eid in sorted(evaluation_ids)}}

    def search(self, payload: dict) -> dict:
        query = payload.get("query", "")
        mode = payload.get("mode", "hybrid")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ArchiveError("请输入不超过4000字的检索问题")
        if mode not in ("hybrid", "semantic", "lexical"):
            raise ArchiveError("Unknown search mode")
        with self.model_lock:
            model = None
            if mode != "lexical":
                if self.model is None:
                    self.model = self.model_factory()
                model = self.model
            return SearchIndex(self.client.archive).search(query, model, mode=mode, domain=payload.get("domain") or None,
                validity=payload.get("validity") or None, include_retracted=payload.get("include_retracted") is True,
                limit=12, max_chars=900, sort=payload.get("sort", "relevance"))

    def pick_path(self, payload):
        kind = payload.get("kind", "file")
        if kind not in ("file", "directory") or not isinstance(payload.get("initial", ""), str):
            raise ArchiveError("Invalid path picker request")
        result = subprocess.run([sys.executable, "-m", "experiment_archive.path_picker", kind,
                                 payload.get("initial", "")], capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ArchiveError("无法打开系统路径选择器：" + result.stderr[-500:])
        return json.loads(result.stdout)

    def save_paths(self, payload):
        with self.jobs_lock:
            if any(job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise ArchiveError("请等待归档任务完成后修改路径")
            action, selected = payload.get("target"), payload.get("path")
            if not isinstance(selected, str) or not selected:
                raise ArchiveError("请选择路径")
            path = Path(selected).resolve()
            if action == "config":
                load_config(path)
                self.config = path
            elif action == "source":
                if not self.config:
                    raise ArchiveError("请先选择同步配置")
                config = load_config(self.config)
                source = next((s for s in config["sources"] if s["id"] == payload.get("id")), None)
                if source is None or not path.exists():
                    raise ArchiveError("来源或路径不存在")
                source["path"] = str(path)
                atomic_write(self.config, canonical(config))
            elif action == "archive":
                if not path.is_dir():
                    raise ArchiveError("归档路径必须为现有文件夹")
                if not (path / "manifest.json").exists() and any(path.iterdir()):
                    raise ArchiveError("请选择现有归档或空文件夹")
                client = ArchiveClient(path)
                client.archive.init()
                client.verify()
                with self.model_lock, self.catalog_lock:
                    self.client = client
                    self.model = None
                    self.translator = TranslationService(client.archive)
                    self.catalog_sha = self.catalog_cache = None
            else:
                raise ArchiveError("Unknown path target")
            atomic_write(self.settings_path, canonical({"archive_path": str(self.client.archive.root),
                         "config_path": str(self.config) if self.config else None}))
        return {"saved": True}

    def start_job(self, kind: str, payload: dict | None = None) -> dict:
        if kind not in ("sync", "preview", "verify", "index", "translate", "accept"):
            raise ArchiveError("Unknown action")
        request = {}
        if kind == "translate":
            payload = payload or {}
            request = {"experiment_id": payload.get("experiment_id"), "target": language(payload.get("target", "zh-CN")),
                       "provider": payload.get("provider", self.translator.config()["provider"]), "revision": payload.get("revision")}
            if not isinstance(request["experiment_id"], str) or request["provider"] not in ("local", "google", "ollama"):
                raise ArchiveError("无效的翻译请求")
            self.client.read(request["experiment_id"], revision=request["revision"])
        if kind in ("sync", "preview") and not (self.config and self.config.is_file()):
            raise ArchiveError("尚未配置项目来源，请在启动时指定 --config")
        with self.jobs_lock:
            if any(job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise ArchiveError("已有归档任务运行，请等待完成")
            identity = uuid.uuid4().hex
            job = {"id": identity, "kind": kind, "status": "queued", "created_at": now(), "result": None, "error": None,
                   "request": request}
            self.jobs[identity] = job
        self.executor.submit(self._run_job, identity)
        return dict(job)

    def _run_job(self, identity: str):
        with self.jobs_lock:
            self.jobs[identity].update(status="running", started_at=now())
            kind = self.jobs[identity]["kind"]
            request = self.jobs[identity]["request"]
        started = time.perf_counter()
        try:
            if kind == "verify":
                result = self.client.verify()
            elif kind == "accept":
                result = self.client.accept()
            elif kind == "preview":
                result = self.client.sync(self.config, dry_run=True)
            elif kind == "translate":
                result = self.translator.record(request["experiment_id"], request["target"], request["provider"], request["revision"])
            else:
                with self.model_lock:
                    if self.model is None:
                        self.model = self.model_factory()
                    if kind == "sync":
                        result = self.client.sync(self.config, embedder_factory=lambda: self.model)
                    else:
                        result = SearchIndex(self.client.archive).build(self.model)
            update = {"status": "completed", "result": result}
        except Exception as exc:
            update = {"status": "failed", "error": str(exc)}
        with self.jobs_lock:
            self.jobs[identity].update(**update, finished_at=now(), seconds=time.perf_counter() - started)

    def job_list(self) -> list[dict]:
        with self.jobs_lock:
            return [dict(job) for job in reversed(list(self.jobs.values()))]


def excerpt(note: str) -> str:
    lines = [line.strip().lstrip("#> ") for line in note.splitlines()
             if line.strip() and not line.startswith(("|", "```", ">"))]
    return " ".join(lines[1:4])[:240]


def first(query: dict, key: str, default=None):
    values = query.get(key)
    return values[0] if values and values[0] != "" else default


def required(query: dict, key: str) -> str:
    value = first(query, key)
    if not value:
        raise ArchiveError(f"Missing {key}")
    return value


def pagination(query: dict) -> tuple[int, int]:
    offset, limit = int(first(query, "offset", "0")), int(first(query, "limit", "20"))
    if offset < 0 or not 1 <= limit <= 200:
        raise ArchiveError("Invalid pagination")
    return offset, limit


def make_server(service: AppService, port: int = 0) -> ThreadingHTTPServer:
    assets = Path(__file__).parent / "ui"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def json(self, value, status=200):
            self.reply(status, canonical(value))

        def handle_request(self, post=False):
            if urlparse(self.path).path == "/api/paths/pick":
                return self.dispatch(post)
            with service.request_lock:
                return self.dispatch(post)

        def dispatch(self, post=False):
            parsed = urlparse(self.path)
            host = self.headers.get("Host", "")
            valid_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in valid_hosts:
                return self.json({"error": "Invalid local host"}, 403)
            if parsed.path.startswith("/api/"):
                if self.headers.get("Authorization") != "Bearer " + service.token:
                    return self.json({"error": "Session expired. Reopen the application."}, 401)
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + name for name in valid_hosts}:
                    return self.json({"error": "Invalid origin"}, 403)
            query = parse_qs(parsed.query)
            try:
                if post:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= size <= 64000:
                        raise ArchiveError("Request too large")
                    payload = json.loads(self.rfile.read(size) or b"{}")
                    if not isinstance(payload, dict):
                        raise ArchiveError("JSON object required")
                    if parsed.path == "/api/search":
                        return self.json(service.search(payload))
                    if parsed.path == "/api/paths/pick":
                        return self.json(service.pick_path(payload))
                    if parsed.path == "/api/paths":
                        return self.json(service.save_paths(payload))
                    if parsed.path == "/api/jobs":
                        return self.json(service.start_job(payload.get("kind", ""), payload), 202)
                    if parsed.path == "/api/translation/settings":
                        return self.json(service.translator.save_settings(payload))
                    if parsed.path == "/api/quit":
                        self.json({"status": "stopping"}, 202)
                        threading.Timer(.2, self.server.shutdown).start()
                        return
                else:
                    if parsed.path == "/api/status":
                        return self.json(service.status())
                    if parsed.path == "/api/experiments":
                        return self.json(service.experiments(query))
                    if parsed.path == "/api/experiment":
                        return self.json(service.detail(required(query, "id"), first(query, "revision")))
                    if parsed.path == "/api/table":
                        return self.json(service.table(query))
                    if parsed.path == "/api/export":
                        output = service.table(query, export=True)
                        return self.reply(200, b"\xef\xbb\xbf" + output["data"], "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{output["filename"]}"'})
                    if parsed.path == "/api/jobs":
                        return self.json({"items": service.job_list()})
                    if parsed.path == "/api/sources":
                        return self.json(load_config(service.config) if service.config and service.config.is_file() else {"sources": []})
                    if parsed.path == "/api/translation/settings":
                        return self.json(service.translator.settings())
                    static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/translation.css": "translation.css", "/icon.svg": "icon.svg"}.get(parsed.path)
                    if static:
                        mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[Path(static).suffix]
                        return self.reply(200, (assets / static).read_bytes(), mime + "; charset=utf-8")
                return self.json({"error": "Not found"}, 404)
            except (ArchiveError, ValueError, KeyError) as exc:
                return self.json({"error": str(exc)}, 400)
            except OSError as exc:
                return self.json({"error": str(exc)}, 500)
            except Exception as exc:
                return self.json({"error": str(exc)}, 500)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(True)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def open_window(url: str, desktop: bool):
    if desktop and sys.platform == "win32":
        candidates = [Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                      Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"]
        for candidate in candidates:
            if candidate.is_file():
                profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExperimentArchive/browser"
                subprocess.Popen([str(candidate), f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                                  "--disable-extensions", "--window-size=1440,960"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
    webbrowser.open(url)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ExperimentArchive 本地可视化App")
    parser.add_argument("--archive", type=Path, default=Path.cwd() / "archive")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--open-url", help="Open an already running local app instance")
    args = parser.parse_args(argv)
    if args.open_url:
        parsed = urlparse(args.open_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            parser.error("--open-url requires a loopback HTTP URL")
        open_window(args.open_url, args.desktop)
        return 0
    service = AppService(args.archive, args.config)
    server = make_server(service, args.port)
    url = f"http://127.0.0.1:{server.server_port}/#" + service.token
    instance = args.archive.resolve() / ".cache/app-instance.json"
    atomic_write(instance, canonical({"pid": os.getpid(), "url": url, "started_at": now()}))
    print(f"ExperimentArchive running at http://127.0.0.1:{server.server_port}/", flush=True)
    if not args.no_open:
        open_window(url, args.desktop)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
        # A later instance may have replaced this receipt.
        try:
            from .store import read_json
            if read_json(instance).get("pid") == os.getpid():
                instance.unlink(missing_ok=True)
        except (ArchiveError, OSError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
