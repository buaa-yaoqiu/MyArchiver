"""Generic installed-archive validation; no project vocabulary or parser required."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from experiment_archive.api import ArchiveClient
from experiment_archive.search import SearchIndex
from experiment_archive.store import atomic_write, canonical, now


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=root / "archive")
    parser.add_argument("--query", help="Optional caller-supplied retrieval smoke query")
    parser.add_argument("--mode", choices=["lexical", "hybrid", "semantic"], default="lexical")
    args = parser.parse_args()
    client = ArchiveClient(args.archive)
    report = {"created_at": now(), "verification": client.verify(),
              "index": SearchIndex(client.archive).status(), "scope": "Generic local archive contracts and integrity"}
    if args.query:
        result = client.evidence(args.query, mode=args.mode, limit=3, rows_per_record=2)
        report["query"] = {"query": args.query, "mode": args.mode,
                           "matched_ids": [r["experiment_id"] for r in result["results"]]}
    atomic_write(root / "reports/core_validation.json", canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
