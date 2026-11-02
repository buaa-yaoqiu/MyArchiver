from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment_archive.importers import publish_import
from experiment_archive.pipeline import direction, evidence, sync
from experiment_archive.store import Archive, ArchiveError, canonical, file_sha
from test_archive import FakeEmbedder, metadata, metric


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = Archive(self.root / "archive")
        self.source = self.root / "source.md"
        self.source.write_text("# 投影实验\n负迁移不能掩盖\n", encoding="utf-8")
        self.config = self.root / "pipeline.json"
        self.config.write_bytes(canonical({"schema_version": 1, "sources": [
            {"id": "notes", "type": "markdown", "path": "source.md", "experiment_id": "exp_note"}]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_creates_no_archive_or_model(self):
        result = sync(self.archive, self.config, dry_run=True,
                      embedder_factory=lambda: self.fail("dry run loaded model"))
        self.assertTrue(result["tasks"][0]["changed"])
        self.assertFalse(self.archive.root.exists())

    def test_first_sync_then_unchanged_skip_and_source_change(self):
        model = FakeEmbedder()
        first = sync(self.archive, self.config, embedder_factory=lambda: model)
        self.assertEqual(first["index"]["encoded"], 1)
        source_sha = file_sha(self.source)
        second = sync(self.archive, self.config, embedder_factory=lambda: model)
        self.assertEqual(second["tasks"][0]["action"], "skipped_unchanged")
        self.assertEqual(second["index"]["action"], "skipped_current")
        self.assertEqual(file_sha(self.source), source_sha)
        self.source.write_text("# 投影实验\n补充置信实验结果\n", encoding="utf-8")
        third = sync(self.archive, self.config, embedder_factory=lambda: model)
        self.assertEqual(third["tasks"][0]["revisions_published"], 1)
        self.assertEqual(third["index"]["encoded"], 1)
        self.assertEqual(len(self.archive.history("exp_note")), 2)

    def test_missing_input_does_not_partially_import_earlier_source(self):
        config = {"schema_version": 1, "sources": [
            {"id": "ok", "type": "markdown", "path": "source.md"},
            {"id": "missing", "type": "markdown", "path": "absent.md"}]}
        self.config.write_bytes(canonical(config))
        with self.assertRaises(ArchiveError):
            sync(self.archive, self.config, index_mode="none")
        self.assertFalse(self.archive.root.exists())

    def test_no_index_mode_works_without_loading_model(self):
        result = sync(self.archive, self.config, index_mode="none",
                      embedder_factory=lambda: self.fail("model loaded"))
        self.assertEqual(result["verification"]["status"], "pass")
        self.assertEqual(result["index"]["action"], "not_requested")

    def test_source_change_during_import_does_not_write_success_receipt(self):
        from experiment_archive.adapters import MarkdownAdapter
        ingest = MarkdownAdapter.ingest
        def changing(*args, **kwargs):
            result = ingest(*args, **kwargs)
            self.source.write_text("changed during publication", encoding="utf-8")
            return result
        with patch.object(MarkdownAdapter, "ingest", new=changing):
            with self.assertRaises(ArchiveError):
                sync(self.archive, self.config, index_mode="none")
        self.assertFalse((self.archive.root / ".cache/pipeline_receipt.json").exists())

    def test_import_refresh_preserves_retraction_and_flags_new_evidence(self):
        self.archive.init()
        meta = metadata()
        meta["source_refs"] = [{"id": "original", "sha256": "old"}]
        publish_import(self.archive, meta, "投影", [metric()])
        updated = metadata()
        updated["source_refs"] = [{"id": "original", "sha256": "changed"}]
        publish_import(self.archive, updated, "投影新证据", [metric()], reason="source changed")
        self.assertEqual(self.archive.record("exp_test")["metadata"]["evidence_validity"], "needs_review")
        old = self.archive.record("exp_test")
        old["metadata"].update(claim_status="retracted", evidence_validity="invalid")
        self.archive.publish(old["metadata"], old["note"], old["metrics"], reason="withdraw")
        publish_import(self.archive, metadata(), "原文重新导入", [metric()], reason="refresh")
        result = self.archive.record("exp_test")["metadata"]
        self.assertEqual(result["claim_status"], "retracted")
        self.assertEqual(result["evidence_validity"], "invalid")

    def test_refresh_preserves_new_attributes_but_updates_explicit_source_config(self):
        self.archive.init()
        old = metadata()
        old["attributes"] = {"optimizer": {"lr": .001, "gradient_clip": {"max_norm": 1.0}}, "manual_tag": "checked"}
        publish_import(self.archive, old, "投影", [metric()])
        updated = metadata()
        updated["attributes"] = {"optimizer": {"lr": .0002}}
        publish_import(self.archive, updated, "投影", [metric()], reason="refresh")
        attrs = self.archive.record("exp_test")["metadata"]["attributes"]
        self.assertEqual(attrs["optimizer"]["lr"], .0002)
        self.assertEqual(attrs["optimizer"]["gradient_clip"]["max_norm"], 1.0)
        self.assertEqual(attrs["manual_tag"], "checked")

    def test_evidence_reads_bounded_rows_with_evaluation_and_source(self):
        self.archive.init()
        self.archive.publish(metadata(), "投影实验负迁移", [metric(), metric(".2", "sq_rel")])
        result = evidence(self.archive, "投影", mode="lexical", domain="c3vd", metric="AbsRel", rows_per_record=1)
        hit = result["results"][0]
        self.assertEqual(hit["metric_rows_total"], 1)
        self.assertEqual(hit["metric_rows"][0]["value"], metric()["value"])
        self.assertIn("eval_test", hit["evaluation_context"])
        self.assertEqual(hit["source_refs"][0]["id"], "fixture")
        self.assertEqual(result["index_action"], "not_used")

    def test_evidence_refreshes_missing_index_and_rejects_when_disabled(self):
        self.archive.init()
        self.archive.publish(metadata(), "投影实验负迁移", [metric()])
        model = FakeEmbedder()
        with self.assertRaises(ArchiveError):
            evidence(self.archive, "投影", refresh_index=False, embedder_factory=lambda: model)
        result = evidence(self.archive, "投影", embedder_factory=lambda: model)
        self.assertEqual(result["index_action"], "refreshed")
        second = evidence(self.archive, "投影", embedder_factory=lambda: model)
        self.assertEqual(second["index_action"], "current")

    def test_metric_direction_and_ci_are_separate_and_delta_sign_correct(self):
        self.assertEqual(direction({"metric": "abs_rel", "delta": ".1", "ci_low": "-.1", "ci_high": ".2"}, metadata()),
                         ("worsened", "includes_zero_not_equivalence"))
        self.assertEqual(direction({"metric": "delta1", "delta": ".1", "ci_low": ".01", "ci_high": ".2"}, metadata()),
                         ("improved", "improved"))
        self.assertEqual(direction({"metric": "unknown", "delta": ".1"}),
                         ("unknown_metric_direction", "unknown_metric_direction"))

    def test_duplicate_pipeline_ids_rejected(self):
        self.config.write_bytes(canonical({"schema_version": 1, "sources": [
            {"id": "same", "type": "markdown", "path": "source.md"},
            {"id": "same", "type": "markdown", "path": "source.md"}]}))
        with self.assertRaises(ArchiveError):
            sync(self.archive, self.config, dry_run=True)


if __name__ == "__main__":
    unittest.main()
