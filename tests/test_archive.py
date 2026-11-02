from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np

from experiment_archive.cli import parser, run
from experiment_archive.importers import import_markdown
from experiment_archive.search import SearchIndex, match_where
from experiment_archive.store import (Archive, ArchiveError, COMPATIBILITY_KEYS, compare_definitions,
                                      read_csv, METRIC_FIELDS)


def metadata(identity="exp_test"):
    return {"schema_version": 1, "experiment_id": identity, "title": "投影约束实验",
            "kind": "test_fixture", "domains": ["c3vd"], "evidence_validity": "valid",
            "claim_status": "current", "verification_basis": "synthetic_test_fixture",
            "attributes": {}, "source_refs": [{"id": "fixture", "description": "synthetic fixture only"}],
            "metric_definitions": {"abs_rel": {"direction": "minimize", "aliases": ["AbsRel"]},
                                   "delta1": {"direction": "maximize", "percentage_points": True}},
            "evaluations": {"eval_test": {"variant_id": "projection", "domain": "c3vd", "prediction_phase": "pre"}}}


def metric(value="0.12345678901234567", metric_name="abs_rel"):
    return {"experiment_id": "exp_test", "evaluation_id": "eval_test", "variant_id": "projection",
            "domain": "c3vd", "scope_type": "domain", "scope_id": "c3vd", "prediction_phase": "pre",
            "metric": metric_name, "value": value, "unit": "dimensionless", "n_frames": 12,
            "value_status": "valid", "source_ref": "fixture"}


class FakeEmbedder:
    fingerprint = "synthetic-test-model-v1"
    identity = {"dimension": 4, "fingerprint": fingerprint, "provider": "test_fixture"}

    def __init__(self):
        self.encoded = 0

    def chunks(self, text):
        return [text]

    def encode(self, texts, query=False):
        if not query:
            self.encoded += len(texts)
        vectors = []
        for text in texts:
            vector = np.array([1 if "投影" in text else .05, 1 if "光度" in text else .05,
                               1 if "置信" in text else .05, .1], dtype=np.float32)
            vector /= np.linalg.norm(vector)
            vectors.append(vector)
        return np.stack(vectors)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = Archive(self.root / "archive")
        self.archive.init()

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, meta=None, note="投影会有负迁移，不能只报告正结果。", rows=None, reason=None):
        return self.archive.publish(meta or metadata(), note, rows if rows is not None else [metric()], reason=reason)

    def test_publish_is_idempotent_and_precision_survives_csv(self):
        first, second = self.publish(), self.publish()
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        record = self.archive.record("exp_test")
        self.assertEqual(record["metrics"][0]["value"], "0.12345678901234567")
        self.assertEqual(self.archive.verify()["metric_rows"], 1)

    def test_revision_requires_reason_and_preserves_old_evidence(self):
        old = self.publish()
        with self.assertRaises(ArchiveError):
            self.publish(note="更正：原配对无效")
        meta = metadata()
        meta.update(evidence_validity="invalid", claim_status="retracted")
        new = self.publish(meta=meta, note="更正：原配对无效", reason="数据身份更正")
        self.assertNotEqual(old["revision"], new["revision"])
        self.assertEqual(self.archive.record("exp_test", revision=old["revision"])["metadata"]["evidence_validity"], "valid")
        self.assertEqual(len(self.archive.history("exp_test")), 2)

    def test_duplicate_metric_rejected_without_publishing(self):
        with self.assertRaises(ArchiveError):
            self.publish(rows=[metric(), metric()])
        self.assertEqual(self.archive.manifest()["generation"], 0)

    def test_unknown_evaluation_and_phase_mismatch_rejected(self):
        row = metric()
        row["evaluation_id"] = "missing"
        with self.assertRaises(ArchiveError):
            self.publish(rows=[row])
        row = metric()
        row["prediction_phase"] = "post"
        with self.assertRaises(ArchiveError):
            self.publish(rows=[row])

    def test_nonfinite_values_rejected(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                self.publish(rows=[metric(value)])

    def test_new_metric_and_nested_attribute_need_no_table_migration(self):
        meta = metadata()
        meta["attributes"] = {"optimizer": {"gradient_clip": {"max_norm": 1.0}}}
        self.publish(meta=meta, rows=[metric(), metric(".9", "new_structure_metric")])
        record = self.archive.record("exp_test")
        self.assertEqual(len(record["metrics"]), 2)
        self.assertTrue(match_where(record["metadata"], "optimizer.gradient_clip.max_norm=1.0"))

    def test_missing_is_unknown_not_false(self):
        self.assertFalse(match_where(metadata(), "optimizer.gradient_clip.max_norm=1"))
        self.assertTrue(match_where(metadata(), "optimizer.gradient_clip.max_norm=1", include_unknown=True))
        self.assertFalse(match_where(metadata(), "optimizer.gradient_clip.enabled=false"))

    def test_tampered_archive_file_is_detected(self):
        self.publish()
        record = self.archive.record("exp_test")
        (Path(record["folder"]) / "metrics.csv").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.archive.verify()

    def test_unsafe_identifier_rejected(self):
        for identity in ("../escape", "a/b", "", ".."):
            meta = metadata(identity)
            with self.subTest(identity=identity), self.assertRaises(ArchiveError):
                self.publish(meta=meta, rows=[])

    def test_writer_lock_prevents_interleaved_publications(self):
        with self.archive.writer():
            with self.assertRaises(ArchiveError):
                self.publish()
        self.assertFalse((self.archive.root / ".write.lock").exists())

    def test_csv_quoting_newline_and_unicode(self):
        row = metric()
        row["source_ref"] = '原文,"字段"\n第二行'
        self.publish(rows=[row])
        self.assertEqual(self.archive.record("exp_test")["metrics"][0]["source_ref"], row["source_ref"])

    def test_markdown_import_does_not_invent_validity_or_metrics(self):
        source = self.root / "history.md"
        text = "# history\n\n## C3VD原实验\nAbsRel .1\n\n## 后续更正\n配对无效\n"
        source.write_text(text, encoding="utf-8")
        result = import_markdown(self.archive, source, split=True)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(source.read_text(encoding="utf-8"), text)
        for record in self.archive.records():
            self.assertEqual(record["metadata"]["evidence_validity"], "unknown")
            self.assertFalse(record["metrics"])

    def test_compare_refuses_mixed_protocol_and_missing_manifests(self):
        left = {key: "same" for key in COMPATIBILITY_KEYS}
        self.assertEqual(compare_definitions(left, left)["status"], "compatible")
        right = dict(left, scoring_signature="old_scoring")
        self.assertEqual(compare_definitions(left, right)["status"], "incompatible")
        right = dict(left, frame_manifest_sha256=None)
        self.assertEqual(compare_definitions(left, right)["status"], "unknown")

    def test_pre_post_and_budget_changes_are_exposed(self):
        left = {key: "same" for key in COMPATIBILITY_KEYS}
        right = dict(left, prediction_phase="post", update_budget=100)
        left.update(prediction_phase="pre", update_budget=20)
        result = compare_definitions(left, right)
        self.assertEqual(set(result["changed_factors"]), {"prediction_phase", "update_budget"})

    def test_incremental_index_reuses_unchanged_vectors(self):
        self.publish()
        model = FakeEmbedder()
        index = SearchIndex(self.archive)
        first = index.build(model)
        second = index.build(model)
        self.assertEqual(first["encoded"], 1)
        self.assertEqual(second["encoded"], 0)
        self.assertEqual(second["reused"], 1)
        self.publish(note="投影加置信，新的实验说明", reason="补充方法")
        third = index.build(model)
        self.assertEqual(third["encoded"], 1)

    def test_stale_index_is_rejected_but_lexical_remains_available(self):
        self.publish()
        index, model = SearchIndex(self.archive), FakeEmbedder()
        index.build(model)
        self.publish(note="投影结果已更正", reason="更正")
        self.assertTrue(index.status()["stale"])
        with self.assertRaises(ArchiveError):
            index.search("投影", model)
        self.assertTrue(index.search("投影", mode="lexical")["results"])

    def test_retracted_results_hidden_by_default_and_available_for_history(self):
        meta = metadata()
        meta.update(evidence_validity="invalid", claim_status="retracted")
        self.publish(meta=meta)
        index = SearchIndex(self.archive)
        default = index.search("投影", mode="lexical")
        self.assertEqual(default["total"], 0)
        self.assertEqual(default["excluded"]["retracted_or_invalid"], 1)
        self.assertEqual(index.search("投影", mode="lexical", include_retracted=True)["total"], 1)

    def test_hybrid_result_contains_provenance_and_negative_outcome(self):
        self.publish()
        index, model = SearchIndex(self.archive), FakeEmbedder()
        index.build(model)
        hit = index.search("投影", model)["results"][0]
        self.assertIn("负迁移", hit["snippet"])
        self.assertEqual(hit["verification_basis"], "synthetic_test_fixture")
        self.assertTrue(hit["revision"])
        self.assertTrue(hit["content_sha256"])

    def test_duplicate_log_identity_collapses_without_resurrecting_retracted_primary(self):
        self.publish()
        alias = metadata("log_alias")
        alias.update(kind="historical_note", canonical_experiment_id="exp_test", evidence_validity="unknown")
        self.archive.publish(alias, "投影投影原始历史记录", [])
        index = SearchIndex(self.archive)
        result = index.search("投影", mode="lexical")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["experiment_id"], "exp_test")
        primary = metadata()
        primary.update(claim_status="retracted", evidence_validity="invalid")
        self.publish(meta=primary, reason="retracted")
        self.assertEqual(index.search("投影", mode="lexical")["total"], 0)

    def test_model_mismatch_and_corrupt_index_are_detected(self):
        self.publish()
        index, model = SearchIndex(self.archive), FakeEmbedder()
        index.build(model)
        model.fingerprint = "other-model"
        with self.assertRaises(ArchiveError):
            index.search("投影", model)
        model.fingerprint = FakeEmbedder.fingerprint
        from experiment_archive.store import read_json
        pointer = read_json(index.cache / "index.json")
        folder = self.archive.root / pointer["path"]
        (folder / "embeddings.npy").write_bytes(b"broken")
        with self.assertRaises(ArchiveError):
            index.search("投影", model)

    def test_cli_variant_filter_and_export(self):
        self.publish()
        args = parser().parse_args(["--archive", str(self.archive.root), "metrics", "exp_test", "--variant", "projection", "--metric", "abs_rel"])
        result = run(args)
        self.assertEqual(result["total"], 1)
        output = self.root / "export.csv"
        args.export = output
        self.assertEqual(run(args)["rows"], 1)
        self.assertEqual(read_csv(output, METRIC_FIELDS)[0]["value"], metric()["value"])

    def test_export_cannot_overwrite_published_evidence(self):
        self.publish()
        target = Path(self.archive.record("exp_test")["folder"]) / "metrics.csv"
        args = parser().parse_args(["--archive", str(self.archive.root), "metrics", "exp_test", "--export", str(target)])
        with self.assertRaises(ArchiveError):
            run(args)
        self.assertEqual(self.archive.verify()["status"], "pass")

    def test_invalid_ci_rejected(self):
        row = {"experiment_id": "exp_test", "candidate_evaluation_id": "eval_test", "baseline_evaluation_id": "eval_test",
               "scope_type": "domain", "scope_id": "c3vd", "domain": "c3vd", "metric": "abs_rel",
               "delta": 0, "ci_low": 1, "ci_high": -1}
        with self.assertRaises(ArchiveError):
            self.archive.publish(metadata(), "note", [metric()], [row])


if __name__ == "__main__":
    unittest.main()
