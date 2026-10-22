"""实验方法和指标口径回归，不调用网络或下载权重。"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.planning import PlanningChoice, PlanningDecision
from app.rag_evaluation import summarize
from app.topk_experiment import TopKExperiment, context_digest, paired_comparisons, summarize_topk


class TopKTests(unittest.TestCase):
    def setUp(self):
        self.case = dict(id="sample", group="general", title="ERP 没权限", description="请处理",
                         expected={"next_step": "clarify", "missing_fact": "previous_access"},
                         relevant_sources=["relevant.md"])
        self.docs = [dict(id=str(i), source="relevant.md" if i == 4 else f"doc{i}.md",
                          snippet=f"上下文 {i}") for i in range(1, 6)]
        self.exp = TopKExperiment.__new__(TopKExperiment)
        self.exp.cfg = SimpleNamespace(decision_min_confidence=.8)

    def test_prefixes_are_nested_and_do_not_mutate_shared_context(self):
        rows = [self.exp.evaluate_prefix(self.case, self.docs, {}, k, 1, live=False) for k in (1, 3, 5)]
        self.assertEqual([r["returned_chunks"] for r in rows], [1, 3, 5])
        self.assertEqual(rows[0]["retrieved"], rows[2]["retrieved"][:1])
        self.assertEqual(len(self.docs), 5)
        self.assertEqual(rows[1]["context_sha256"], context_digest(self.docs[:3]))

    def test_relevant_at_rank_four_is_hit_at_k_but_not_hit_at_three(self):
        row = self.exp.evaluate_prefix(self.case, self.docs, {}, 5, 1, live=False)
        self.assertTrue(row["hit_at_k"])
        self.assertFalse(row["hit_at_3"])
        self.assertEqual(row["reciprocal_rank"], .25)

    def test_labels_are_not_passed_to_model_and_call_is_scored(self):
        choice = PlanningChoice(next_step="clarify", action=None, resource="erp", permission=None,
            missing_fact="previous_access", user_evidence=[], sop_refs=[], reason="需要使用历史", confidence=.6)
        self.exp.model = Mock()
        self.exp.model.plan.return_value = PlanningDecision(choice, "deepseek", {"usage": {"total_tokens": 21}})
        row = self.exp.evaluate_prefix(self.case, self.docs, {}, 3, 1, live=True)
        args = self.exp.model.plan.call_args.args
        self.assertEqual(len(args), 3)
        self.assertEqual(args[2], self.docs[:3])
        self.assertNotIn("expected", args[0].__dict__)
        self.assertNotIn("relevant_sources", args[0].__dict__)
        self.assertTrue(row["passed"])
        self.assertIsNone(row["citation_integrity"])
        summary = summarize_topk([row])[0]
        self.assertEqual(summary["planning_success_rate"], 1)
        self.assertEqual(summary["tokens"], 21)

    def test_retrieval_only_has_no_model_accuracy_or_token_claim(self):
        row = self.exp.evaluate_prefix(self.case, self.docs, {}, 3, 1, live=False)
        summary = summarize_topk([row])[0]
        for field in ("planning_success_rate", "passed", "tokens", "raw_route_matches", "citation_integrity_rate"):
            self.assertIsNone(summary[field], field)

    def test_e2e_unmeasured_metrics_are_not_zero(self):
        row = {"variant": "hybrid", "group": "general", "measurement_phase": "e2e", "passed": True,
               "planning": {"metadata": {"usage": {"total_tokens": 99}}}}
        summary = summarize([row])[0]
        self.assertEqual(summary["passed"], 1)
        # 单次 planning 用量不能冒充整个端到端流程的用量。
        for field in ("tokens", "raw_route_matches", "grounded_correct", "wrong_proposals", "hit_at_3"):
            self.assertIsNone(summary[field], field)

    def test_partial_token_usage_is_explicit(self):
        rows = [{"variant": "hybrid", "group": "general", "decision": {"metadata": {"usage": {"total_tokens": 10}}}},
                {"variant": "hybrid", "group": "general", "error": "ModelTimeoutError"}]
        result = summarize(rows)[0]
        self.assertIsNone(result["tokens"])
        self.assertEqual(result["tokens_observed"], 10)
        self.assertEqual(result["token_usage_n"], 1)

    def test_pairs_track_context_changes_and_do_not_drop_failures(self):
        rows = [dict(id="a", repeat=1, top_k=3, context_sha256="same", passed=True, measurement_phase="planning"),
                dict(id="a", repeat=1, top_k=1, context_sha256="same", passed=False, error="Timeout", measurement_phase="planning"),
                dict(id="b", repeat=1, top_k=3, context_sha256="b", passed=False, measurement_phase="planning"),
                dict(id="b", repeat=1, top_k=1, context_sha256="c", passed=True, measurement_phase="planning")]
        result = paired_comparisons(rows, [1, 3])[0]
        self.assertEqual(result["paired_n"], 2)
        self.assertEqual(result["different_context_n"], 1)
        self.assertEqual(result["improved"], ["b#r1"])
        self.assertEqual(result["regressed"], ["a#r1"])
        self.assertEqual(result["error_pair_n"], 1)

    def test_vector_fallback_aborts_instead_of_claiming_bge(self):
        self.exp.kb = Mock()
        self.exp.kb.search_with_status.return_value = (self.docs, {"backend": "bm25", "degraded": True})
        with self.assertRaises(RuntimeError):
            self.exp.prepare(self.case, 5)


if __name__ == "__main__":
    unittest.main()
