import unittest
from app.planning import PlanningChoice
from app.rag_evaluation import score_decision


class RagScoringTests(unittest.TestCase):
    def choice(self, confidence):
        return PlanningChoice(next_step="clarify", action=None, resource="erp", permission=None,
            missing_fact="symptom", user_evidence=[], sop_refs=[], reason="需要更多信息", confidence=confidence)

    def test_low_confidence_question_remains_question(self):
        score = score_decision(self.choice(.6), {"next_step": "manual_review"})
        self.assertFalse(score["raw_valid_correct"])
        self.assertFalse(score["passed"])
        self.assertEqual(score["effective_next_step"], "clarify")

    def test_correct_question_is_distinct_from_gate_intervention(self):
        score = score_decision(self.choice(.6), {"next_step": "clarify", "missing_fact": "symptom"})
        self.assertTrue(score["raw_valid_correct"])
        self.assertTrue(score["passed"])

    def test_low_confidence_execution_still_becomes_manual_review(self):
        proposal = self.choice(.6).model_copy(update={"next_step": "propose_action", "action": "open_incident", "missing_fact": None})
        score = score_decision(proposal, {"next_step": "manual_review"})
        self.assertFalse(score["raw_valid_correct"])
        self.assertTrue(score["passed"])
        self.assertEqual(score["effective_next_step"], "manual_review")

    def test_invalid_citation_never_counts_as_correct_controller_routing(self):
        score = score_decision(self.choice(.6), {"next_step": "manual_review"}, valid=False)
        self.assertFalse(score["passed"])
        self.assertEqual(score["effective_next_step"], "failed")


if __name__ == "__main__":
    unittest.main()
