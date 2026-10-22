import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import get_settings
from app.investigation import (InvestigationTools, InvestigationRunner, InvestigationStep,
                               Citation, validate_citations)
from app.investigation_experiment import evaluate

ROOT = Path(__file__).resolve().parents[1]


def choose(tool, argument=""):
    return InvestigationStep(kind="tool", tool=tool, argument=argument, assessment=None, runbook_id=None,
                             summary="", next_check="", citations=[], reason="补充缺失证据")


class ScriptModel:
    source = "test"

    def __init__(self, *steps):
        self.steps = iter(steps)

    def choose(self, *args, **kwargs):
        return next(self.steps), {}


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cases = json.loads((ROOT/"evals/it_investigation.json").read_text(encoding="utf-8"))["cases"]
        self.case = self.cases[0]
        self.path = self.root/"events.json"
        self.path.write_text(json.dumps(self.case["telemetry"]), encoding="utf-8")
        self.docs = ROOT/"data/diagnostics/runbooks.json"
        self.adapter = InvestigationTools(self.path, self.docs)

    def tearDown(self):
        self.tmp.cleanup()

    def finish(self):
        doc = next(r for r in json.loads(self.docs.read_text(encoding="utf-8")) if r["id"] == "kb-v01")
        server = self.case["telemetry"]["resources"]["vpn"]["accounts"]["alice"]["sessions"]["session-731"][0]
        return InvestigationStep(kind="finish", tool=None, argument="", assessment="supported_hypothesis", runbook_id="kb-v01",
            summary="当前会话存在域名解析异常线索。", next_check="请网络支持核查当前 DNS 配置。", reason="关联当前日志与规程",
            citations=[Citation(record_id=server["id"], quote=server["text"]), Citation(record_id=doc["id"], quote=doc["text"])])

    def model(self):
        return ScriptModel(choose("client_events"), choose("session_events", "session-731"),
                           choose("search_runbooks", "DNS NXDOMAIN"), self.finish())

    def run_model(self, model, **kwargs):
        return InvestigationRunner(self.adapter, model, **kwargs).run(text=self.case["request"], owner="alice", binding="x")

    def test_followup_query_and_supported_citations_are_persisted_as_hypothesis(self):
        result = self.run_model(self.model())
        self.assertEqual(result["finding"], "kb-v01")
        self.assertTrue(result["evidence_verified"])
        self.assertFalse(result["verified"])  # 逐字引用不是对语义结论的程序证明。
        self.assertEqual(result["tool_calls"], 3)

    def test_unobserved_or_other_owner_session_cannot_be_read(self):
        for observations, owner, ref in (([], "alice", "session-731"), ([], "bob", "session-731"),
                                          ([], "alice", "../../auth.keys.json")):
            with self.assertRaises(ValueError):
                self.adapter.read("session_events", ref, owner, observations, timeout=1)

    def test_resource_scoped_retrieval_does_not_return_erp_docs_to_vpn(self):
        obs = self.adapter.read("search_runbooks", "ERP token audience DNS", "alice", [], timeout=1)
        self.assertTrue(all(r["id"].startswith("kb-v") for r in obs["records"]))

    def test_write_tools_extra_identity_and_path_are_not_in_schema(self):
        for bad in ({**choose("client_events").model_dump(), "tool": "run_sql"},
                    {**choose("client_events").model_dump(), "owner": "admin"}):
            result = self.run_model(ScriptModel(bad))
            self.assertEqual(result["tool_calls"], 0)
            self.assertTrue(result["stop_reason"].startswith("model_error"))

    def test_null_unused_tool_text_is_normalized_but_finish_is_strict(self):
        raw={**choose("client_events").model_dump(),"summary":None,"next_check":None}
        step=InvestigationStep.model_validate(raw)
        self.assertEqual(step.summary,"")
        with self.assertRaises(ValueError):
            InvestigationStep.model_validate({**self.finish().model_dump(),"summary":None})

    def test_invented_citations_do_not_produce_accepted_attachment(self):
        result = self.run_model(ScriptModel(self.finish()))
        self.assertEqual(result["stop_reason"], "invalid_citation")
        self.assertFalse(result["evidence_verified"])

    def test_repeated_query_and_tool_budget_stop(self):
        result = self.run_model(ScriptModel(choose("client_events"), choose("client_events")))
        self.assertEqual(result["stop_reason"], "repeated_tool")
        result = self.run_model(self.model(), max_tools=1)
        self.assertEqual(result["stop_reason"], "tool_budget")
        self.assertEqual(result["tool_calls"], 1)

    def test_checkpoint_crash_resumes_read_and_keeps_budget(self):
        records=[]
        def save(session):
            records.append(copy.deepcopy(session))
            if session["pending_tool"]:
                raise SystemExit("crash")
        with self.assertRaises(SystemExit):
            InvestigationRunner(self.adapter, ScriptModel(choose("client_events"))).run(
                text="VPN", owner="alice", binding="x", checkpoint=save)
        model = ScriptModel(choose("session_events", "session-731"), choose("search_runbooks", "DNS NXDOMAIN"), self.finish())
        result = InvestigationRunner(self.adapter, model).run(text="VPN", owner="alice", binding="x", saved=records[-1])
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["finding"], "kb-v01")

    def test_snapshot_change_is_not_combined_with_old_logs(self):
        def save(session):
            if session["observations"]:
                raw=json.loads(self.path.read_text(encoding="utf-8"))
                raw["version"] = 2
                self.path.write_text(json.dumps(raw), encoding="utf-8")
        result=InvestigationRunner(self.adapter,self.model()).run(text="VPN",owner="alice",binding="x",checkpoint=save)
        self.assertEqual(result["stop_reason"], "model_finish")
        self.assertEqual(len({o["snapshot_id"] for o in result["observations"]}), 1)

    def test_same_completed_work_is_reused_without_read(self):
        result=self.run_model(self.model())
        with patch.object(self.adapter,"read",side_effect=AssertionError("duplicate")):
            again=InvestigationRunner(self.adapter,ScriptModel()).run(text="VPN",owner="alice",binding="x",saved=result)
        self.assertIs(again,result)

    def test_legacy_in_progress_snapshot_degrades_without_restarting_budget(self):
        legacy={"binding":"x","owner":"alice","source":"deepseek","status":"running","tool_calls":2,"model_calls":3}
        with patch.object(self.adapter,"read") as read:
            result=InvestigationRunner(self.adapter,ScriptModel()).run(text="VPN",owner="alice",binding="x",saved=legacy)
        read.assert_not_called()
        self.assertEqual(result["format"],"investigation_v1")
        self.assertEqual(result["previous_strategy"]["tool_calls"],2)
        self.assertEqual(result["stop_reason"],"scope_or_strategy_changed")

    def test_switch_back_to_structured_diagnostics_does_not_misread_new_state(self):
        from app.diagnostics import DiagnosticRunner, RuleDiagnosticModel
        saved={"format":"investigation_v1","binding":"x","owner":"alice","status":"running","tool_calls":4,"model_calls":5}
        with patch.object(self.adapter,"read") as read:
            result=DiagnosticRunner(self.adapter,RuleDiagnosticModel()).run(text="VPN",owner="alice",binding="x",saved=saved)
        read.assert_not_called()
        self.assertEqual(result["stop_reason"],"configuration_changed")
        self.assertEqual(result["previous_strategy"]["model_calls"],5)

    def test_expired_investigation_does_not_read_or_restart_deadline(self):
        snapshots=[]
        def checkpoint(session):
            snapshots.append(copy.deepcopy(session))
            if session["pending_tool"]:
                raise SystemExit("crash")
        with self.assertRaises(SystemExit):
            InvestigationRunner(self.adapter,ScriptModel(choose("client_events")),clock=lambda:0,deadline_seconds=10).run(
                text="VPN",owner="alice",binding="x",checkpoint=checkpoint)
        with patch.object(self.adapter,"read") as read:
            result=InvestigationRunner(self.adapter,ScriptModel(),clock=lambda:11).run(
                text="VPN",owner="alice",binding="x",saved=snapshots[-1])
        read.assert_not_called()
        self.assertEqual(result["stop_reason"],"deadline")

    def test_linked_session_is_exposed_only_after_reading_its_parent(self):
        branch=json.loads((ROOT/"evals/it_investigation_branches.json").read_text(encoding="utf-8"))["cases"][0]
        self.path.write_text(json.dumps(branch["telemetry"]),encoding="utf-8")
        adapter=InvestigationTools(self.path,self.docs,"erp")
        client=adapter.read("client_events","","alice",[],timeout=1)
        with self.assertRaises(ValueError):
            adapter.read("session_events","hop-17","alice",[client],timeout=1)
        parent=adapter.read("session_events","gateway-704","alice",[client],timeout=1)
        child=adapter.read("session_events","hop-17","alice",[client,parent],timeout=1)
        self.assertEqual(child["records"][0]["id"],"g-leaf")

    def test_failed_model_abstention_is_not_scored_as_task_success(self):
        from app.errors import ModelOutputError
        unknown=next(c for c in self.cases if c["id"]=="t10")
        cfg=get_settings(_env_file=None,runbooks_file=self.docs)
        with patch("app.investigation.InvestigationModel.choose",side_effect=ModelOutputError()):
            row=evaluate(unknown,"fixed_llm",cfg)
        self.assertTrue(row["label_match"])
        self.assertFalse(row["contract_passed"])

    def test_rule_baseline_covers_development_cases(self):
        cfg=get_settings(_env_file=None,runbooks_file=self.docs)
        for case in self.cases:
            if case["split"] == "dev":
                with self.subTest(case=case["id"]):
                    self.assertTrue(evaluate(case,"rules",cfg)["contract_passed"])

    def test_production_workflow_selects_resource_specific_investigator(self):
        from app.audit import AuditStore
        from app.auth import Principal
        from app.knowledge import KnowledgeBase
        from app.model_adapter import DeepSeekDecisionModel, RuleBasedModel
        from app.workflow import EnterpriseAgent
        from app.tools import EnterpriseTools
        from app.service import TicketService
        cfg=get_settings(_env_file=None,data_dir=self.root,investigation_file=self.path,runbooks_file=self.docs,
                         diagnosis_mode="react",diagnosis_backend="investigation",rag_backend="lexical",
                         runbooks_dir=ROOT/"data/knowledge/runbooks", runbook_strategy="bm25", diagnosis_prefetch=False)
        audit=AuditStore(cfg.db_path)
        agent=EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root/"kb",backend="lexical"),audit),audit,
                              DeepSeekDecisionModel("test-only"),settings=cfg)
        # 主流程使用确定性替身，实际选择器仍必须识别 DeepSeek 主模型并构造新调查器。
        rules=RuleBasedModel()
        with patch.object(agent.model,"clarify",side_effect=rules.clarify), patch.object(agent.model,"plan",side_effect=rules.plan), \
             patch("app.investigation.InvestigationModel.choose",side_effect=self.model().choose):
            state=TicketService(agent).create(Principal(user_id="alice",role="employee",department="IT"),
                title="VPN 故障",description="VPN 连接失败，报错超时")
            self.assertEqual(state.ticket.status, "awaiting_investigation")
            state=agent.investigate(state.ticket.id)
        self.assertEqual(state.ticket.status,"awaiting_support")
        self.assertEqual(state.ticket.diagnostic_report["format"],"investigation_v1")
        self.assertEqual(state.ticket.diagnostic_report["finding"],"kb-v01")
        self.assertIn("vpn",agent.investigation_runners)


if __name__ == "__main__":
    unittest.main()
