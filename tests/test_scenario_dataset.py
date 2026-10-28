"""Dataset integrity and real adapter isolation, without live credentials or writes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import get_settings
from app.scenario_catalog import DATA_GAPS, DIAGNOSTICS, WORKFLOWS
from app.scenario_dataset import ScenarioDatasetBuilder, ScenarioDatasetEvaluator, ScenarioDatasetValidator


ROOT = Path(__file__).resolve().parents[1]


class ScenarioDatasetTests(unittest.TestCase):
    def setUp(self):
        self.builder = ScenarioDatasetBuilder()

    def test_reproducible_unique_examples_and_group_splits(self):
        first = self.builder.build()
        self.assertEqual(first, self.builder.build())
        result = ScenarioDatasetValidator.validate_rows(*first)
        self.assertEqual(result["cases"], len(DIAGNOSTICS) * 10 + len(DATA_GAPS) * 4 + len(WORKFLOWS) * 4)
        self.assertGreaterEqual(len({r["scenario"] for r in first[1]}), 130)

    def test_label_leak_and_cross_split_variant_are_rejected(self):
        inputs, labels = self.builder.build()
        leaked = deepcopy(inputs)
        leaked[0]["setup"]["expected_runbook"] = "kb-v01"
        with self.assertRaisesRegex(ValueError, "leaked"):
            ScenarioDatasetValidator.validate_rows(leaked, labels)
        labels[1]["split"] = "unrelated"
        with self.assertRaisesRegex(ValueError, "crosses"):
            ScenarioDatasetValidator.validate_rows(inputs, labels)

    def test_release_preserved_and_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "v1"
            self.builder.write(target, ROOT / "data/knowledge/runbooks")
            self.assertGreater(ScenarioDatasetValidator().validate(target)["cases"], 800)
            with self.assertRaises(FileExistsError):
                self.builder.write(target, ROOT / "data/knowledge/runbooks")
            with (target / "inputs.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                ScenarioDatasetValidator().validate(target)

    def test_partial_or_unknown_evidence_does_not_force_known_answer(self):
        inputs, labels = self.builder.build()
        for label in labels:
            if label["variant"] == "partial_evidence":
                self.assertEqual(label["expected"]["allowed_runbooks"], [])
                self.assertEqual(label["expected"]["allowed_assessments"], ["insufficient"])
        unknown = next(r for r in labels if r["scenario"] == "erp_gateway_timeout" and r["variant"] == "current")
        self.assertEqual(unknown["expected"]["allowed_runbooks"], [])
        self.assertTrue(all(r["human_review"] is None for r in labels))

    def test_clock_and_expiry_evidence_have_current_reference(self):
        spec = next(r for r in DIAGNOSTICS if r.key == "vpn_certificate_expired")
        case, label = self.builder.diagnosis(spec, "utc_timestamp", 3)
        account = next(v for k, v in case["telemetry"]["resources"]["vpn"]["accounts"].items() if v["owner_id"] == case["owner"])
        event = next(iter(account["sessions"].values()))[0]
        self.assertIn("+00:00", event["event_at"])
        self.assertIn("notBefore=", event["text"])
        self.assertIn("notAfter=", event["text"])
        self.assertIn("reference_clock=", event["text"])

    def test_actual_adapter_data_gaps_and_foreign_scope(self):
        evaluator = ScenarioDatasetEvaluator(get_settings(llm_mode="offline"))
        for key, code, phase in DATA_GAPS:
            with self.subTest(key=key):
                case, label = self.builder.gap(key, code, phase, 0)
                row = evaluator.check_adapter(case, label)
                self.assertTrue(row["passed"], row)
                self.assertFalse(row["diagnostic_quality_measured"])

    def test_future_capabilities_are_explicit_specifications(self):
        _, labels = self.builder.build()
        future = [r for r in labels if r["readiness"] != "existing_contract"]
        self.assertTrue(future)
        self.assertTrue(all(r["evaluation_kind"] == "workflow_specification" for r in future))

    def test_model_receives_request_and_observations_without_labels(self):
        from app.investigation import InvestigationStep
        spec = next(s for s in DIAGNOSTICS if s.key == "erp_gateway_timeout")
        case, label = self.builder.diagnosis(spec, "current", 0)
        captured = []
        class Model:
            source = "deepseek"
            def choose(self, text, observations, remaining_tools, *, timeout):
                captured.append(json.dumps({"text": text, "observations": observations}))
                return InvestigationStep(kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
                    summary="当前只取得网关超时记录，具体原因需要核查。", next_check="由支持人员补采当前请求的应用指标。",
                    citations=[], reason="当前内部证据不足"), {}
        with patch("app.investigation.InvestigationModel", return_value=Model()):
            row = ScenarioDatasetEvaluator(get_settings(llm_mode="offline")).live(case, label)
        self.assertTrue(row["contract_passed"])
        self.assertTrue(captured)
        self.assertNotIn("allowed_assessments", captured[0])
        self.assertNotIn("required_log_ids", captured[0])
        self.assertIsNone(row["human_review"])


if __name__ == "__main__":
    unittest.main()
