"""Contracts must work for non-medical projects with no integration imported."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment_archive.adapters import load_adapter, MarkdownAdapter
from experiment_archive.api import ArchiveClient, ExperimentRecord
from experiment_archive.pipeline import direction, sync
from experiment_archive.store import ArchiveError, canonical, csv_bytes, METRIC_FIELDS, COMPARISON_FIELDS


def classification_record():
    metadata = {"schema_version": 1, "experiment_id": "classifier_trial", "title": "文本分类器小样本实验",
        "domains": ["news"], "attributes": {"model": "linear"}, "kind": "classification",
        "evidence_validity": "valid", "claim_status": "current",
        "source_refs": [{"id": "export", "description": "synthetic interface test"}],
        "metric_definitions": {"macro_f1": {"direction": "maximize", "aliases": ["F1"]},
                               "latency_ms": {"direction": "minimize", "aliases": ["延迟"]}},
        "search_aliases": {"分类": "classification classifier"},
        "evaluations": {"eval_news": {"variant_id": "linear", "domain": "news", "prediction_phase": "test"}}}
    rows = [{"experiment_id": "classifier_trial", "evaluation_id": "eval_news", "variant_id": "linear",
        "domain": "news", "scope_type": "domain", "scope_id": "news_test", "prediction_phase": "test",
        "metric": name, "value": value, "unit": unit, "n_frames": "", "value_status": "valid", "source_ref": "export"}
        for name, value, unit in (("macro_f1", ".81", "ratio"), ("latency_ms", "3.2", "ms"))]
    return ExperimentRecord(metadata, "分类器取得F1提升，另报告推理延迟。", rows)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sdk_can_publish_query_and_read_classification_metrics(self):
        client = ArchiveClient(self.root / "archive")
        client.publish(classification_record())
        self.assertEqual(client.table("classifier_trial", metric="F1")["rows"][0]["value"], ".81")
        result = client.evidence("分类器", mode="lexical", metric="延迟", domain="news")
        self.assertEqual(result["results"][0]["metric_rows"][0]["metric"], "latency_ms")
        self.assertEqual(client.verify()["metric_rows"], 2)

    def test_generic_bundle_interface_roundtrips_without_project_adapter(self):
        bundle = self.root / "export"
        bundle.mkdir()
        record = classification_record()
        (bundle / "metadata.json").write_bytes(canonical(record.metadata))
        (bundle / "note.md").write_text(record.note, encoding="utf-8")
        (bundle / "metrics.csv").write_bytes(csv_bytes(record.metrics, METRIC_FIELDS))
        (bundle / "comparisons.csv").write_bytes(csv_bytes([], COMPARISON_FIELDS))
        client = ArchiveClient(self.root / "archive")
        self.assertEqual(client.ingest(bundle)["records_processed"], 1)
        self.assertFalse(client.ingest(bundle)["changed"])
        self.assertEqual(client.read("classifier_trial")["metadata"]["domains"], ["news"])

    def test_direction_is_declared_by_project_not_metric_spelling(self):
        row = {"metric": "macro_f1", "delta": ".05", "ci_low": ".01", "ci_high": ".1"}
        self.assertEqual(direction(row), ("unknown_metric_direction", "unknown_metric_direction"))
        self.assertEqual(direction(row, classification_record().metadata), ("improved", "improved"))
        row["metric"] = "latency_ms"
        self.assertEqual(direction(row, classification_record().metadata), ("worsened", "worsened"))

    def test_conflicting_aliases_and_bad_metric_rules_rejected(self):
        for definition in ({"direction": "bigger"}, {"aliases": [1]}, {"percentage_points": "yes"}):
            record = classification_record()
            record.metadata["metric_definitions"]["macro_f1"] = definition
            with self.assertRaises(ArchiveError):
                ArchiveClient(self.root / "archive").publish(record)
        record = classification_record()
        record.metadata["metric_definitions"]["latency_ms"]["aliases"] = ["F1"]
        with self.assertRaises(ArchiveError):
            ArchiveClient(self.root / "archive").publish(record)

    def test_unknown_adapter_fails_without_loading_unrelated_plugins(self):
        with patch("experiment_archive.adapters.entry_points", return_value=[]):
            with self.assertRaisesRegex(ArchiveError, "not installed"):
                load_adapter("not-installed")
            self.assertIsInstance(load_adapter("markdown"), MarkdownAdapter)

    def test_external_adapter_discovered_through_entrypoint(self):
        class ExternalAdapter:
            api_version = 1
            def inputs(self, path, options):
                return [path]
            def ingest(self, archive, path, options, reason):
                from experiment_archive.importers import import_markdown
                result = import_markdown(archive, path, reason=reason, domain_terms=["news"])
                return {"records_processed": result["imported"]}
        class Entry:
            def load(self):
                return ExternalAdapter
        source = self.root / "source.md"
        source.write_text("# news 分类器", encoding="utf-8")
        config = self.root / "pipeline.json"
        config.write_bytes(canonical({"schema_version": 1, "sources": [
            {"id": "third_party", "adapter": "external", "path": "source.md", "options": {}}]}))
        with patch("experiment_archive.adapters.entry_points", return_value=[Entry()]):
            result = sync(ArchiveClient(self.root / "archive").archive, config, index_mode="none")
        self.assertEqual(result["tasks"][0]["records_processed"], 1)

    def test_bad_adapter_api_version_rejected(self):
        class BadAdapter:
            api_version = 999
        class Entry:
            def load(self):
                return BadAdapter
        with patch("experiment_archive.adapters.entry_points", return_value=[Entry()]):
            with self.assertRaisesRegex(ArchiveError, "SourceAdapter v1"):
                load_adapter("external")

    def test_core_imports_without_optional_project_package(self):
        code = '''
import importlib.abc, sys, tempfile
class BlockProject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("experiment_archive_endoscope"):
            raise RuntimeError("Core attempted to import optional project")
sys.meta_path.insert(0, BlockProject())
from experiment_archive.api import ArchiveClient, ExperimentRecord
from experiment_archive.cli import parser
from experiment_archive.adapters import load_adapter
with tempfile.TemporaryDirectory() as folder:
    client = ArchiveClient(folder)
    client.publish(ExperimentRecord({"schema_version":1,"experiment_id":"test","title":"Generic",
        "source_refs":[{"id":"example"}],"domains":[]}, "generic record"))
    assert client.evidence("generic", mode="lexical")["total"] == 1
assert load_adapter("bundle").api_version == 1
'''
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
