import json
from pathlib import Path
import unittest

from app.config import get_settings
from app.diagnostic_experiment import evaluate_case, summarize


class DiagnosticExperimentTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads((Path(__file__).resolve().parents[1]/"evals/vpn_diagnostics.json").read_text(encoding="utf-8"))["cases"]
        self.cfg = get_settings(_env_file=None, llm_mode="offline")

    def test_all_authored_cases_and_fixed_baseline_agree_on_offline_contract(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                for variant in ("fixed", "adaptive_rule"):
                    row = evaluate_case(case, variant, 1, self.cfg)
                    self.assertTrue(row["passed"])
                    self.assertEqual(row["llm_calls"], 0)
                    self.assertEqual(row["total_tokens"], 0)
                    self.assertEqual(row["session"]["source"], "rule")

    def test_original_baseline_does_not_invent_diagnostics_or_llm_cost(self):
        row = evaluate_case(self.cases[0], "original", 1, self.cfg)
        self.assertEqual(row["finding"], "inconclusive")
        self.assertEqual(row["tool_calls"], 0)
        self.assertEqual(row["llm_calls"], 0)
        self.assertFalse(row["passed"])

    def test_missing_usage_is_not_reported_as_zero(self):
        row = evaluate_case(self.cases[0], "fixed", 1, self.cfg)
        row["total_tokens"] = None
        result = summarize([row])[0]
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(result["usage_missing_rows"], 1)

    def test_pairs_have_same_user_description_but_different_backend_observations(self):
        self.assertEqual(len({c["request"] for c in self.cases}), 1)
        self.assertEqual(len({c["expected_finding"] for c in self.cases}), 5)
