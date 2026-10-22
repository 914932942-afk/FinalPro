"""混合检索与证据边界，不调用真实模型或下载权重。"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.models import AgentState, Ticket
from app.planning import PlanningChoice, validate_choice, rule_plan
from app.policy import analyze_request


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = get_settings(_env_file=None, llm_mode="offline", data_dir=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def doc(self, name, resource, code, body, status="active"):
        meta = dict(resource=resource, codes=code, status=status, version="v1")
        (self.root / name).write_text('<!-- rag: ' + json.dumps(meta) + ' -->\n# 处理规则\n' + body, encoding="utf-8")

    def store(self):
        return KnowledgeBase(self.root, backend="chroma", settings=self.cfg)

    def test_same_code_wrong_resource_does_not_pollute_results(self):
        self.doc("erp.md", "erp", "E417", "ERP E417 身份冻结，人工核查。")
        self.doc("vpn.md", "vpn", "E417", "VPN E417 连接故障，登记事件。")
        kb = self.store()
        wrong = kb.bm25_search("VPN E417", 10)
        with patch.object(kb, "_vector_search", return_value=wrong):
            docs, status = kb.search_with_status("ERP 报错 E417")
        self.assertEqual([d["source"] for d in docs], ["erp.md"])
        self.assertEqual(status["strategy"], "hybrid")
        self.assertIn("bm25", docs[0]["ranks"])

    def test_retired_procedure_is_not_loaded(self):
        self.doc("old.md", "erp", "E417", "旧处理路径。", status="retired")
        self.assertEqual(self.store().documents, [])

    def test_metadata_revision_changes_citation_identity(self):
        self.doc("erp.md", "erp", "E417", "处理路径。")
        first = self.store().documents[0]
        path = self.root / "erp.md"
        path.write_text(path.read_text(encoding="utf-8").replace('"v1"', '"v2"'), encoding="utf-8")
        second = self.store().documents[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["version"], "v2")

    def test_heading_chunks_do_not_mix_procedures(self):
        (self.root / "doc.md").write_text("# 第一条\nERP 操作。\n# 第二条\nVPN 操作。", encoding="utf-8")
        docs = self.store().documents
        self.assertEqual(len(docs), 2)
        self.assertNotIn("VPN", docs[0]["text"])
        self.assertNotIn("ERP", docs[1]["text"])

    def test_vector_failure_preserves_filtered_bm25_and_degradation(self):
        self.doc("erp.md", "erp", "E417", "ERP E417 人工核查。")
        self.doc("vpn.md", "vpn", "E417", "VPN E417 登记故障。")
        kb = self.store()
        with patch.object(kb, "_vector_search", side_effect=RuntimeError):
            docs, status = kb.search_with_status("ERP 报错 E417")
        self.assertTrue(status["degraded"])
        self.assertEqual(status["backend"], "bm25")
        self.assertEqual([d["source"] for d in docs], ["erp.md"])

    def state_and_choice(self):
        ticket = Ticket("VPN 报错", "E417", "alice", "IT", owner_id="alice")
        state = AgentState(ticket)
        state.understanding = analyze_request(ticket).to_dict()
        conclusion = "处理结论：propose_action，动作 open_incident，资源 vpn。"
        state.retrieved_docs = [dict(id="x", resource="vpn", codes="E417", status="active", snippet=conclusion)]
        choice = PlanningChoice(next_step="propose_action", action="open_incident", resource="vpn", permission=None,
            missing_fact=None, user_evidence=["E417"], sop_refs=["x"], sop_evidence={"x": conclusion}, reason="登记", confidence=.95)
        return state, choice

    def test_quote_must_be_exact_and_present(self):
        state, choice = self.state_and_choice()
        self.assertEqual(validate_choice(state, choice, require_quotes=True).resource, "vpn")
        for quotes in ({}, {"x": "申请权限。"}, {"fake": "登记网关故障。"}):
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                validate_choice(state, choice.model_copy(update={"sop_evidence": quotes}), require_quotes=True)

    def test_citation_scope_status_and_code_are_rechecked(self):
        for changes in ({"resource": "erp"}, {"codes": "E562"}, {"status": "retired"}, {"codes": ""}):
            state, choice = self.state_and_choice()
            state.retrieved_docs[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_choice(state, choice, require_quotes=True)

    def test_offline_rules_do_not_guess_internal_codes(self):
        state, _ = self.state_and_choice()
        result = rule_plan(state.ticket, analyze_request(state.ticket), state.retrieved_docs)
        self.assertEqual(result.choice.next_step, "manual_review")

    def test_real_quote_cannot_support_opposite_action(self):
        state, choice = self.state_and_choice()
        state.retrieved_docs[0]["snippet"] = "处理结论：manual_review。停止自动处理。"
        choice.sop_evidence["x"] = state.retrieved_docs[0]["snippet"]
        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_choice(state, choice, require_quotes=True)

    def test_quoting_background_instead_of_handling_conclusion_is_rejected(self):
        state, choice = self.state_and_choice()
        state.retrieved_docs[0]["snippet"] += "\n这是一条 VPN 规程。"
        choice.sop_evidence["x"] = "这是一条 VPN 规程。"
        with self.assertRaisesRegex(ValueError, "handling conclusion"):
            validate_choice(state, choice, require_quotes=True)

    def test_handling_sentence_suffices_without_unrelated_trailing_explanation(self):
        state, choice = self.state_and_choice()
        state.retrieved_docs[0]["snippet"] += "登记任务不等于故障修复完成。"
        self.assertEqual(validate_choice(state, choice, require_quotes=True).action, "open_incident")

    def test_unquoted_conflicting_procedure_cannot_be_hidden(self):
        state, choice = self.state_and_choice()
        state.retrieved_docs.append({**state.retrieved_docs[0], "id": "conflict", "snippet": "处理结论：manual_review。"})
        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_choice(state, choice, require_quotes=True)

    def test_missing_declared_condition_blocks_even_valid_quote(self):
        state, choice = self.state_and_choice()
        state.retrieved_docs[0]["snippet"] += "\n前置事实：previous_access=yes"
        with self.assertRaisesRegex(ValueError, "prerequisite"):
            validate_choice(state, choice, require_quotes=True)
        state.ticket.description += "，昨天还能用"
        state.understanding = analyze_request(state.ticket).to_dict()
        self.assertEqual(validate_choice(state, choice, require_quotes=True).action, "open_incident")

    def test_unrelated_or_retired_conflict_does_not_veto_valid_procedure(self):
        for changes in ({"resource": "erp"}, {"codes": "E999"}, {"status": "retired"}):
            state, choice = self.state_and_choice()
            state.retrieved_docs.append({**state.retrieved_docs[0], "id": "other", "snippet": "处理结论：manual_review。", **changes})
            with self.subTest(changes=changes):
                self.assertEqual(validate_choice(state, choice, require_quotes=True).action, "open_incident")


if __name__ == "__main__":
    unittest.main()
