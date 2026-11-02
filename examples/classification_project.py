"""Non-medical project using only the public archive API (synthetic demo values)."""
from __future__ import annotations

import argparse
import json
from experiment_archive.api import ArchiveClient, ExperimentRecord


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, help="Use a separate demo archive path")
    args = parser.parse_args()
    client = ArchiveClient(args.archive)
    record = ExperimentRecord(
        metadata={"schema_version": 1, "experiment_id": "example_classifier", "title": "分类项目接口示例",
            "domains": ["news"], "kind": "synthetic_demo", "attributes": {},
            "verification_basis": "synthetic_demo_not_real_experiment", "evidence_validity": "unknown",
            "source_refs": [{"id": "example", "description": "Synthetic demonstration values"}],
            "metric_definitions": {"macro_f1": {"direction": "maximize", "aliases": ["F1"]}},
            "evaluations": {"example_eval": {"variant_id": "linear", "domain": "news", "prediction_phase": "test"}}},
        note="演示如何由项目上报结果；0.81是合成示例数值，不是真实实验成绩。",
        metrics=[{"experiment_id": "example_classifier", "evaluation_id": "example_eval", "variant_id": "linear",
            "domain": "news", "scope_type": "domain", "scope_id": "news_test", "prediction_phase": "test",
            "metric": "macro_f1", "value": "0.81", "unit": "ratio", "n_frames": "",
            "value_status": "valid", "source_ref": "example"}])
    publication = client.publish(record)
    print(json.dumps({"published": publication, "metrics": client.table("example_classifier", metric="F1")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
