"""Cross-module acceptance: scoped knowledge, durable handoff, and independent feedback."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.audit import AuditStore
from app.auth import Principal
from app.config import get_settings
from app.errors import AuthorizationError, IdempotencyConflict, OperationPending
from app.investigation import InvestigationTools, InvestigationRunner, InvestigationStep, validate_citations
from app.knowledge import KnowledgeBase
from app.model_adapter import DeepSeekDecisionModel, RuleBasedModel
from app.runbook_knowledge import RunbookKnowledge
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from test_investigation import ScriptModel, choose

ROOT = Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case = json.loads((ROOT/"evals/it_investigation.json").read_text(encoding="utf-8"))["cases"][0]
        self.events = self.root/"events.json"
        self.events.write_text(json.dumps(self.case["telemetry"]), encoding="utf-8")
        self.cfg = get_settings(_env_file=None, data_dir=self.root, investigation_file=self.events,
            runbooks_dir=ROOT/"data/knowledge/runbooks", rag_backend="lexical", runbook_strategy="bm25", diagnosis_background=False)
        self.knowledge = RunbookKnowledge(self.cfg.runbooks_dir, settings=self.cfg)
        self.adapter = InvestigationTools(self.events, self.cfg.runbooks_file, knowledge=self.knowledge)
        self.alice = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT", support_groups=["network", "applications", "data", "service_desk"])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def insufficient():
        return InvestigationStep(kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
            summary="需要更多当前会话证据。", next_check="请支持人员补充本次解析查询结果。", citations=[], reason="证据不足")

    def service(self):
        audit = AuditStore(self.cfg.db_path)
        agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root/"kb", backend="lexical"), audit), audit,
                                DeepSeekDecisionModel("test-only"), settings=self.cfg)
        return TicketService(agent), agent

    def create(self, service, agent):
        rules = RuleBasedModel()
        with patch.object(agent.model, "clarify", side_effect=rules.clarify), patch.object(agent.model, "plan", side_effect=rules.plan):
            return service.create(self.alice, title="VPN 故障", description="VPN 连接失败，报错超时", idempotency_key="one")

    def complete(self, service, agent, state):
        with patch("app.investigation.InvestigationModel.choose", return_value=(self.insufficient(), {})):
            return service.investigate(self.alice, state.ticket.id)

    def test_acceptance_does_not_wait_for_model_and_update_has_separate_key(self):
        service, agent = self.service()
        with patch("app.investigation.InvestigationModel.choose", side_effect=AssertionError("must not investigate yet")):
            state = self.create(service, agent)
        self.assertEqual(state.ticket.status, "awaiting_investigation")
        self.assertEqual(state.action_result.status, "succeeded")
        done = self.complete(service, agent, state)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.investigation_status, "completed")
        self.assertNotEqual(done.action_result.idempotency_key, done.enrichment_result["idempotency_key"])
        self.assertIsNone(done.resolution["external_issue_resolved"])
        with patch.object(agent.tools.adapter, "execute", side_effect=AssertionError("duplicate create")), \
             patch.object(agent.tools.adapter, "attach_evidence", side_effect=AssertionError("duplicate update")):
            self.assertEqual(service.investigate(self.alice, state.ticket.id).to_dict(), done.to_dict())
            self.assertEqual(self.create(service, agent).ticket.status, "awaiting_investigation")  # exact original response

    def test_restart_can_finish_queued_work_without_recreating_incident(self):
        service, agent = self.service()
        state = self.create(service, agent)
        restarted, new_agent = self.service()
        with patch.object(new_agent.tools.adapter, "execute", side_effect=AssertionError("duplicate")):
            done = self.complete(restarted, new_agent, state)
        self.assertEqual(done.ticket.status, "awaiting_support")

    def test_feedback_authorization_revision_replay_and_original_hypothesis(self):
        service, agent = self.service()
        done = self.complete(service, agent, self.create(service, agent))
        args = (done.ticket.id, 1, "rejected", "人工定位了其他原因", "trace-731 排查记录", True)
        with self.assertRaises(AuthorizationError):
            service.feedback(self.alice, *args)
        service.claim(self.manager, done.ticket.id, 0)
        verified = service.feedback(self.manager, *args)
        self.assertEqual(verified.ticket.diagnostic_report, done.ticket.diagnostic_report)
        self.assertIsNone(verified.resolution["external_issue_resolved"])
        self.assertEqual(verified.ticket.status, "awaiting_verification")
        self.assertEqual(service.feedback(self.manager, *args).to_dict(), verified.to_dict())
        with self.assertRaises(IdempotencyConflict):
            service.feedback(self.manager, done.ticket.id, 1, "accepted", "different", "record", True)
        with self.assertRaises(IdempotencyConflict):
            service.feedback(self.manager, done.ticket.id, 3, "accepted", "skip", "record", True)

    def test_feedback_cannot_precede_investigation(self):
        service, agent = self.service()
        state = self.create(service, agent)
        with self.assertRaises(IdempotencyConflict):
            service.feedback(self.manager, state.ticket.id, 1, "accepted", "too early", "record", True)

    def test_unknown_attachment_never_retries_write_or_recreates_incident(self):
        service, agent = self.service()
        state = self.create(service, agent)
        with patch.object(agent.tools.adapter, "attach_evidence", side_effect=TimeoutError):
            done = self.complete(service, agent, state)
        self.assertEqual(done.ticket.status, "manual_review")
        self.assertEqual(done.action_result.status, "succeeded")
        self.assertEqual(done.ticket.investigation_status, "attachment_unknown")
        with patch.object(agent.tools.adapter, "execute", side_effect=AssertionError("duplicate")), \
             patch.object(agent.tools.adapter, "attach_evidence", side_effect=AssertionError("unsafe retry")):
            service.recover(self.manager, state.ticket.id)

    def test_crash_after_attachment_recovers_from_ledger(self):
        service, agent = self.service()
        state = self.create(service, agent)
        persist = agent._persist
        def crash(state, node, *args, **kwargs):
            if node == "enrich":
                raise SystemExit("crash after downstream success")
            return persist(state, node, *args, **kwargs)
        with patch.object(agent, "_persist", side_effect=crash), self.assertRaises(SystemExit):
            self.complete(service, agent, state)
        with patch.object(agent.tools.adapter, "attach_evidence", side_effect=AssertionError("duplicate")):
            done = service.recover(self.manager, state.ticket.id)
        self.assertEqual(done.ticket.status, "awaiting_support")

    def test_prefetch_removes_a_model_turn_without_increasing_reads(self):
        old = InvestigationRunner(self.adapter, ScriptModel(choose("client_events"), self.insufficient())).run(
            text="VPN", owner="alice", binding="old")
        new = InvestigationRunner(self.adapter, ScriptModel(self.insufficient()), prefetch=True).run(
            text="VPN", owner="alice", binding="new")
        self.assertEqual((old["tool_calls"], new["tool_calls"]), (1, 1))
        self.assertEqual((old["model_calls"], new["model_calls"]), (2, 1))

    def test_vpn_and_erp_share_one_knowledge_service_but_not_observations(self):
        service, agent = self.service()
        vpn = self.complete(service, agent, self.create(service, agent))
        rules = RuleBasedModel()
        with patch.object(agent.model, "clarify", side_effect=rules.clarify), patch.object(agent.model, "plan", side_effect=rules.plan):
            erp = service.create(self.alice, title="ERP 故障", description="ERP 打不开，页面报错 504", idempotency_key="erp")
        self.assertEqual(erp.ticket.status, "awaiting_investigation")
        erp = self.complete(service, agent, erp)
        self.assertIs(agent.investigation_runners["vpn"].adapter.knowledge,
                      agent.investigation_runners["erp"].adapter.knowledge)
        self.assertEqual(vpn.diagnosis["evidence_snapshot"]["resource"], "vpn")
        self.assertEqual(erp.diagnosis["evidence_snapshot"]["resource"], "erp")

    def test_shared_service_sees_runbooks_published_after_an_empty_first_request(self):
        import shutil
        service, agent = self.service()
        directory = self.root / "initially-empty-runbooks"
        agent.settings = self.cfg.model_copy(update={"runbooks_dir":directory})
        first = self.complete(service, agent, self.create(service, agent))
        self.assertEqual(first.diagnosis["stop_reason"], "runbooks_not_configured")
        shutil.copyfile(ROOT/"data/knowledge/runbooks/kb-v00.md", directory/"kb-v00.md")
        rules = RuleBasedModel()
        with patch.object(agent.model, "clarify", side_effect=rules.clarify), patch.object(agent.model, "plan", side_effect=rules.plan):
            second = service.create(self.alice, title="VPN 故障", description="VPN 连接失败，报错超时", idempotency_key="after-publish")
        done = self.complete(service, agent, second)
        self.assertEqual(done.diagnosis["stop_reason"], "model_finish")
        self.assertEqual(len(done.diagnosis["evidence_snapshot"]["runbooks"]), 1)

    def test_snapshot_scope_excludes_other_accounts_and_freezes_related_updates(self):
        snapshot = self.adapter.capture("alice")
        raw = copy.deepcopy(self.case["telemetry"])
        raw["resources"]["vpn"]["accounts"]["bob"] = {"client_events": [], "sessions": {}}
        self.events.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.adapter.capture("alice")["snapshot_id"], snapshot["snapshot_id"])
        raw["resources"]["vpn"]["accounts"]["alice"]["client_events"][0]["text"] = "new evidence"
        self.events.write_text(json.dumps(raw), encoding="utf-8")
        frozen = self.adapter.read("client_events", "", "alice", [], timeout=1, snapshot=snapshot)
        self.assertNotEqual(frozen["records"][0]["text"], "new evidence")
        self.assertNotEqual(self.adapter.capture("alice")["snapshot_id"], snapshot["snapshot_id"])
        self.assertNotIn("bob", json.dumps(snapshot))

    def test_scoped_snapshot_and_prefetch_survive_crash(self):
        saved=[]
        def crash(session):
            saved.append(copy.deepcopy(session))
            if session["pending_tool"]:
                raise SystemExit()
        with self.assertRaises(SystemExit):
            InvestigationRunner(self.adapter, ScriptModel(), prefetch=True).run(
                text="VPN", owner="alice", binding="x", checkpoint=crash)
        self.events.write_text('{"simulated":true,"resources":{}}', encoding="utf-8")
        result=InvestigationRunner(self.adapter, ScriptModel(self.insufficient()), prefetch=True).run(
            text="VPN", owner="alice", binding="x", saved=saved[-1])
        self.assertTrue(result["observations"][0]["records"])
        self.assertEqual(result["tool_calls"],1)

    def test_workflow_resume_uses_frozen_evidence_when_source_disappears(self):
        service,agent=self.service()
        state=self.create(service,agent)
        with patch("app.investigation.InvestigationModel.choose",side_effect=SystemExit("crash")), self.assertRaises(SystemExit):
            service.investigate(self.alice,state.ticket.id)
        before=agent.audit.get_state(state.ticket.id)["diagnosis"]
        self.assertTrue(before["evidence_snapshot"])
        self.events.unlink()  # this test's temporary file only
        restarted,new_agent=self.service()
        done=self.complete(restarted,new_agent,state)
        self.assertEqual(done.ticket.status,"awaiting_support")
        self.assertEqual(done.diagnosis["observations"],before["observations"])

    def test_conflict_is_marked_even_when_only_one_candidate_is_returned(self):
        rows=self.knowledge.capture("vpn")
        duplicate={**rows[0],"id":"conflict-v2","version":"v3"}
        result,_=self.knowledge.search(rows[0]["text"], [rows[0],duplicate],limit=1)
        self.assertTrue(result[0]["conflicted"])

    def test_specific_deployment_cannot_be_inferred_from_runbook_or_query(self):
        from app.investigation import Citation
        doc=next(r for r in self.knowledge.capture("erp") if r["id"]=="kb-e04")
        log={"id":"log","kind":"log","text":"connection pool timeout; health probe succeeds"}
        record={**doc,"kind":"runbook"}
        step=InvestigationStep(kind="finish",tool=None,argument="",assessment="supported_hypothesis",runbook_id=doc["id"],
            summary="待核实的租约问题",next_check="采集租约指标",reason="test",citations=[
                Citation(record_id="log",quote=log["text"]),Citation(record_id=doc["id"],quote=doc["text"][:100])])
        with self.assertRaisesRegex(ValueError,"deployment"):
            validate_citations(step,[{"records":[log,record]}])
        log["text"]="deployment=cloud-proxy; "+log["text"]
        validate_citations(step,[{"records":[log,record]}])
        conflicting={"id":"other-log","kind":"log","text":"deployment=classic-direct; another observation"}
        with self.assertRaisesRegex(ValueError,"conflicting"):
            validate_citations(step,[{"records":[log,record,conflicting]}])

    def test_policy_lookup_returns_all_applicable_conflicting_fragments(self):
        kb=KnowledgeBase(ROOT/"data/knowledge", backend="lexical", settings=self.cfg)
        docs,_=kb.planning_context("VPN E417")
        self.assertIn("local/vpn_e417.md", {r["source"] for r in docs})
        self.assertNotIn("local/erp_e417.md", {r["source"] for r in docs})
        self.assertFalse(any(r.get("kind")=="runbook" for r in docs))
        candidates=kb.search("VPN 网关会话队列异常", 12)
        self.assertIn("local/vpn_e417.md", {r["source"] for r in candidates})

    def test_whole_runbook_keeps_conditions_and_source(self):
        rows=self.knowledge.capture("vpn")
        result,_=self.knowledge.search("DNS NXDOMAIN",rows)
        dns=next(r for r in result if r["id"]=="kb-v01")
        self.assertIn("不能仅凭连接超时",dns["text"])
        self.assertIn("证据与交接",dns["text"])
        self.assertTrue(dns["content_hash"])

    def test_new_investigation_sees_revisions_old_snapshot_stays_unchanged(self):
        import shutil
        directory=self.root/"runbooks"
        shutil.copytree(self.cfg.runbooks_dir,directory)
        knowledge=RunbookKnowledge(directory,settings=self.cfg)
        before=knowledge.capture("vpn")
        path=directory/"kb-v01.md"
        path.write_text(path.read_text(encoding="utf-8").replace("demo-v2","demo-v3").replace("网络支持组","演示网络二线"),encoding="utf-8")
        after=knowledge.capture("vpn")
        old=next(r for r in before if r["id"]=="kb-v01")
        new=next(r for r in after if r["id"]=="kb-v01")
        self.assertNotEqual(old["content_hash"],new["content_hash"])
        self.assertNotIn("演示网络二线",old["text"])
        self.assertIn("演示网络二线",new["text"])

    def test_vector_failure_is_visible_on_runbook_fallback(self):
        with patch.object(self.knowledge.kb,"_encode",side_effect=RuntimeError):
            records,status=self.knowledge.search("DNS NXDOMAIN",self.knowledge.capture("vpn"),strategy="hybrid")
        self.assertTrue(status["degraded"])
        self.assertEqual(status["strategy"],"bm25")
        self.assertTrue(records)

    def test_observed_deployment_filters_wrong_scope_without_changing_diagnosis_rules(self):
        rows=self.knowledge.capture("erp")
        records,status=self.knowledge.search("ERP 报表 等待数据库连接 连接池 超时",rows,
                                            environments=["classic-direct"])
        self.assertNotIn("kb-e04",{r["id"] for r in records})
        self.assertIn("kb-e05",{r["id"] for r in records})
        self.assertEqual(status["observed_environments"],["classic-direct"])

    def test_query_cannot_override_observed_environment(self):
        snapshot=self.adapter.capture("alice")
        observations=[{"records":[{"id":"source","kind":"log",
            "text":"endpoint=self-managed; DNS name resolution failed"}]}]
        result=self.adapter.read("search_runbooks","endpoint=managed-terminal DNS 域名后缀", "alice", observations,
                                 timeout=1,snapshot=snapshot)
        self.assertNotIn("kb-v04",{r["id"] for r in result["records"]})

    def test_feedback_http_scope_and_queued_response_contract(self):
        from fastapi.testclient import TestClient
        from app.api import create_app
        from auth_helpers import demo_identities, auth_headers
        service,agent=self.service()
        identities,keys=demo_identities(self.root)
        state=self.complete(service,agent,self.create(service,agent))
        body=dict(revision=1,verdict="inconclusive",note="补采证据",evidence="人工记录",issue_resolved=None,case_revision=state.ticket.case_revision)
        with TestClient(create_app(agent,identities=identities)) as client:
            url=f"/tickets/{state.ticket.id}/feedback"
            self.assertEqual(client.post(url,json=body).status_code,401)
            self.assertEqual(client.post(url,json=body,headers=auth_headers(keys,"bob")).status_code,403)
            self.assertEqual(client.post(url,json=body,headers=auth_headers(keys)).status_code,403)
            self.assertEqual(client.post(url,json={**body,"actor":"admin"},headers=auth_headers(keys,"manager")).status_code,422)
            self.assertEqual(client.post(f"/portal/tickets/{state.ticket.id}/claim", json={"revision":0}, headers=auth_headers(keys,"manager")).status_code,200)
            body["case_revision"]=agent.audit.get_ticket(state.ticket.id)["case_revision"]
            response=client.post(url,json=body,headers=auth_headers(keys,"manager"))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["ticket"]["feedback"][0]["actor"],"manager")

    def test_pending_attachment_after_crash_is_not_replayed(self):
        service,agent=self.service()
        state=self.create(service,agent)
        with patch.object(agent.tools.adapter,"attach_evidence",side_effect=SystemExit("unknown downstream outcome")), self.assertRaises(SystemExit):
            self.complete(service,agent,state)
        with patch.object(agent.tools.adapter,"attach_evidence",side_effect=AssertionError("must not retry")):
            done=service.recover(self.manager,state.ticket.id)
        self.assertEqual(done.ticket.status,"manual_review")
        self.assertEqual(done.ticket.investigation_status,"attachment_unknown")

    def test_legacy_pending_diagnosis_does_not_skip_incident_creation(self):
        service,agent=self.service()
        state=self.create(service,agent)
        raw=state.to_dict();raw["workflow_version"]=1;raw["action_result"]=None
        agent.audit.save_state(raw)
        with self.assertRaises(IdempotencyConflict):
            service.recover(self.manager,state.ticket.id)

    def test_legacy_successful_execution_does_not_enter_new_background_stage(self):
        service,agent=self.service()
        state=self.create(service,agent)
        raw=state.to_dict();raw["workflow_version"]=1;raw["next_node"]="execute"
        agent.audit.save_state(raw)
        with patch.object(agent.tools.adapter,"execute",side_effect=AssertionError("must reuse ledger")):
            done=service.recover(self.manager,state.ticket.id)
        self.assertEqual(done.ticket.status,"awaiting_support")
        self.assertIsNone(done.next_node)

    def test_real_http_response_arrives_while_background_model_is_blocked(self):
        import socket
        import threading
        import time
        import urllib.request
        import uvicorn
        from app.api import create_app
        from auth_helpers import demo_identities, auth_headers
        service,agent=self.service()
        agent.settings=self.cfg.model_copy(update={"diagnosis_background":True})
        identities,keys=demo_identities(self.root)
        release=threading.Event(); entered=threading.Event()
        def blocked(*args,**kwargs):
            entered.set()
            release.wait(15)
            return self.insufficient(),{}
        sock=socket.socket();sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_app(agent,identities=identities),log_level="error",lifespan="on"))
        thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
        rules=RuleBasedModel()
        try:
            with patch.object(agent.model,"clarify",side_effect=rules.clarify), patch.object(agent.model,"plan",side_effect=rules.plan), \
                 patch("app.investigation.InvestigationModel.choose",side_effect=blocked):
                thread.start()
                deadline=time.monotonic()+5
                while not server.started and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(server.started)
                request=urllib.request.Request(f"http://127.0.0.1:{port}/tickets",
                    data=json.dumps({"title":"VPN 故障","description":"VPN 连接失败，报错超时"}).encode(),
                    headers={**auth_headers(keys),"Content-Type":"application/json"},method="POST")
                with urllib.request.urlopen(request,timeout=5) as response:
                    accepted=json.load(response)
                self.assertFalse(release.is_set())
                self.assertEqual(accepted["ticket"]["status"],"awaiting_investigation")
                self.assertTrue(entered.wait(3))
                release.set()
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if agent.audit.get_ticket(accepted["ticket"]["id"])["status"]=="closed":
                        break
                    time.sleep(.02)
                self.assertEqual(agent.audit.get_ticket(accepted["ticket"]["id"])["status"],"awaiting_support")
        finally:
            release.set();server.should_exit=True
            if thread.ident:
                thread.join(6)
            sock.close()


if __name__ == "__main__":
    unittest.main()
