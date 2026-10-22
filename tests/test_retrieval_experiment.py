import unittest

from app.retrieval_experiment import score, summarize
from app.knowledge_experiment import observed_environments


class RetrievalScoringTests(unittest.TestCase):
    def test_scope_is_taken_from_logs_not_query_or_expected_label(self):
        case = {"query":"deployment=wrong", "logs":[{"text":"deployment=right; observed"}]}
        self.assertEqual(observed_environments(case), ["right"])

    def test_scope_leak_and_recall_are_independent(self):
        case = {"logs":[{"text":"endpoint=right; observed"}],"expected_runbook":"a"}
        docs = [{"id":"a","environment":"right"},{"id":"b","environment":"wrong"}]
        result = score(case, docs, docs)
        self.assertTrue(result["recalled"])
        self.assertEqual(result["scope_leaks"], ["b"])

    def test_no_answer_is_not_counted_as_a_retrieval_success_or_generated_failure(self):
        case = {"logs":[],"expected_runbook":None}
        result = score(case,[{"id":"a"}],[{"id":"a"}])
        self.assertIsNone(result["recalled"])
        self.assertIsNone(result["reciprocal_rank"])
        self.assertEqual(result["unanswerable_candidates"],1)

    def test_summary_excludes_unanswerable_cases_from_recall_denominator(self):
        base = dict(variant="hybrid",top_k=2,group="spoken",scope_leaks=[],retrieval={},
                    warm_uncached_ms=[1,2,3],context_characters=5)
        rows = [{**base,"recalled":True,"reciprocal_rank":.5},
                {**base,"recalled":None,"reciprocal_rank":None}]
        result = summarize(rows)[0]
        self.assertEqual(result["answerable_n"],1)
        self.assertEqual(result["recalled"],1)
        self.assertEqual(result["mrr"],.5)
