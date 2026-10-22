import unittest
from app.knowledge_experiment import Answer, literal_checks, Judgment, grounded_handoff


class ScoringTests(unittest.TestCase):
    def test_real_quotes_do_not_turn_repair_claim_into_success(self):
        records=[{"id":"x","text":"NXDOMAIN for current request"}]
        answer=Answer(summary="域名解析正常，故障已经解决。",next_check="无需检查。",handoff="",
                      citations={"x":records[0]["text"]})
        checks=literal_checks(answer,records)
        self.assertTrue(checks["citation_integrity"])
        self.assertTrue(checks["claims_completed_repair"])

    def test_citation_integrity_and_semantic_review_are_separate(self):
        answer=Answer(summary="当前可能是 DNS 故障，未执行修复。",next_check="核对解析结果。",handoff="网络组",
                      citations={"fake":"invented evidence"})
        self.assertFalse(literal_checks(answer,[])["citation_integrity"])
        self.assertFalse(literal_checks(answer,[])["claims_completed_repair"])

    def test_semantic_judge_cannot_return_truthy_strings(self):
        with self.assertRaises(ValueError):
            Judgment.model_validate(dict(evidence_supported="false", applicable_procedure=True,
                actionable_handoff=True,uncertainty_preserved=True,reason="bad format"))

    def test_plausible_advice_without_target_procedure_does_not_count_as_grounded_handoff(self):
        case={"expected_runbook":"kb","logs":[{"id":"log","text":"DNS failed"}]}
        answer=Answer(summary="可能是解析问题",next_check="建议检查 DNS",citations={"log":"DNS failed"})
        self.assertFalse(grounded_handoff(answer,case,[],True))

    def test_handoff_requires_current_log_and_applicable_procedure_citations(self):
        case={"expected_runbook":"kb","logs":[{"id":"log","text":"DNS failed"}]}
        docs=[{"id":"kb","text":"Check resolver"}]
        answer=Answer(summary="可能是解析问题",next_check="检查 DNS",assessment="supported_hypothesis",runbook_id="kb",
                      citations={"kb":"Check resolver","log":"DNS failed"})
        self.assertTrue(grounded_handoff(answer,case,docs,True))
        self.assertFalse(grounded_handoff(answer,case,docs,False))
        answer.citations.pop("log")
        self.assertFalse(grounded_handoff(answer,case,docs,True))

    def test_unanswerable_requires_explicit_insufficient_and_no_selected_procedure(self):
        case={"expected_runbook":None,"logs":[]}
        answer=Answer(summary="缺少证据",next_check="补充日志")
        self.assertTrue(grounded_handoff(answer,case,[],True))
        answer.assessment="supported_hypothesis"
        self.assertFalse(grounded_handoff(answer,case,[],True))

    def test_repair_veto_respects_negation_but_not_a_following_positive_clause(self):
        for text in ("尚不能确定根因或已完成修复。", "不能声称故障已经解决。", "没有证据表明已经修复。"):
            self.assertFalse(literal_checks(Answer(summary=text,next_check="继续检查"),[])["claims_completed_repair"])
        for text in ("尚未验证，但已经修复。", "不能声称已完成修复；故障已经解决。", "问题已经修复。"):
            self.assertTrue(literal_checks(Answer(summary=text,next_check="继续检查"),[])["claims_completed_repair"])
