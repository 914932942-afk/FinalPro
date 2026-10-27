"""Explicit identity links, service-account isolation and actionable data gaps."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.api import create_app
from app.business_accounts import AccountDirectory, DiagnosticDataGap, demo_bindings, initialize_demo_bindings
from app.diagnostics import DiagnosticRunner, LocalVPNDiagnostics, RuleDiagnosticModel, incident_attachment
from app.errors import IdempotencyConflict
from app.investigation import InvestigationTools, InvestigationRunner, InvestigationStep
from app.model_adapter import DeepSeekDecisionModel, RuleBasedModel
from app.models import AgentState
from app.portal import present_ticket
from app.service import TicketService
from app.workflow import EnterpriseAgent
from auth_helpers import auth_headers
import test_case_flow as flow_fixtures
import test_http_actions as http_fixtures
from test_investigation import ScriptModel, choose

ROOT = Path(__file__).resolve().parents[1]


class BusinessAccountTests(unittest.TestCase):
    def setUp(self):
        self.env = flow_fixtures.CaseFlowTests("test_registered_incident_remains_open_and_can_be_resolved_without_report")
        self.env.setUp()
        self.root = self.env.root
        self.path = self.env.cfg.account_bindings_file
        self.directory = self.env.agent.account_directory
        self.events = self.root / "events.json"
        self.telemetry = json.loads((ROOT / "data/diagnostics/it_events.json").read_text(encoding="utf-8"))
        self.save_events()
        self.bob = self.env.identities.principal("bob")

    def tearDown(self):
        self.env.tearDown()

    def save_events(self):
        self.events.write_text(json.dumps(self.telemetry), encoding="utf-8")

    def update_binding(self, owner="bob", resource="erp", **changes):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        next(row for row in data["bindings"] if row["user_id"] == owner and row["resource"] == resource).update(changes)
        temporary = self.path.with_suffix(".next")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(self.path)

    def remove_binding(self, owner="bob", resource="erp"):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["bindings"] = [row for row in data["bindings"] if (row["user_id"], row["resource"]) != (owner, resource)]
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def adapter(self, resource="erp"):
        return InvestigationTools(self.events, ROOT / "data/diagnostics/runbooks.json", resource, accounts=self.directory)

    @staticmethod
    def insufficient():
        return InvestigationStep(kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
            summary="当前记录不足以确认根因，请支持人员继续核查。", next_check="核对当前账号对应会话的后续处理记录。", citations=[], reason="保留证据边界")

    def runner(self, adapter=None, model=None):
        return InvestigationRunner(adapter or self.adapter(), model or ScriptModel(self.insufficient()), prefetch=True)

    def modern_service(self):
        cfg = self.env.cfg.model_copy(update={"diagnosis_mode": "react", "diagnosis_backend": "investigation",
            "investigation_file": self.events, "runbooks_dir": ROOT / "data/knowledge/runbooks", "diagnosis_background": False})
        agent = EnterpriseAgent(self.env.agent.tools, self.env.audit, DeepSeekDecisionModel("test-only"), settings=cfg)
        return TicketService(agent, self.env.identities), agent

    def create_incident(self, service, agent, *, owner=None):
        rules = RuleBasedModel()
        with patch.object(agent.model, "clarify", side_effect=rules.clarify), patch.object(agent.model, "plan", side_effect=rules.plan):
            return service.create(owner or self.bob, title="ERP 故障", description="ERP 以前能使用，现在打开页面报错 503。")

    def test_explicit_demo_links_include_business_ids_different_from_login_ids(self):
        scope = self.directory.resolve("bob", "erp")
        self.assertEqual(scope["account_id"], "erp.bob")
        self.assertEqual(scope["owner_id"], "bob")
        self.assertEqual(scope["code"], "ready")
        sample = json.loads((ROOT / "data/diagnostics/account_bindings.example.json").read_text(encoding="utf-8"))
        self.assertEqual(sample, demo_bindings())
        before = self.path.read_bytes()
        initialize_demo_bindings(self.path, self.env.identities.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mapping_missing_unbound_and_invalid_are_distinct_and_never_guessed(self):
        self.assertEqual(self.directory.resolve("not-provisioned", "erp")["code"], "account_unbound")
        self.assertIsNone(self.directory.resolve("not-provisioned", "erp")["account_id"])
        self.path.unlink()
        self.assertEqual(self.directory.resolve("bob", "erp")["code"], "mapping_not_configured")
        self.path.write_text("invalid-json", encoding="utf-8")
        self.assertEqual(self.directory.resolve("bob", "erp")["code"], "mapping_invalid")

    def test_duplicate_owner_resource_and_shared_account_ownership_fail_closed(self):
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for duplicate in (deepcopy(original["bindings"][0]), {"user_id": "someone", "resource": "erp", "account_id": "erp.bob", "enabled": True}):
            data = deepcopy(original)
            data["bindings"].append(duplicate)
            self.path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(self.directory.resolve("bob", "erp")["code"], "mapping_invalid")

    def test_other_accounts_mapping_updates_do_not_invalidate_my_scope(self):
        before = self.directory.resolve("bob", "erp")
        self.update_binding("alice", "vpn", account_id="vpn.alice.new")
        self.assertEqual(before, self.directory.resolve("bob", "erp"))
        self.directory.validate(before)

    def test_erp_account_lookup_uses_mapped_id_and_preserves_portal_owner(self):
        adapter = self.adapter()
        snapshot = adapter.capture("bob")
        self.assertEqual(snapshot["account_scope"]["account_id"], "erp.bob")
        observed = adapter.read("client_events", "", "bob", [], timeout=1, snapshot=snapshot)
        self.assertEqual(observed["owner"], "bob")
        self.assertEqual(observed["account_id"], "erp.bob")
        self.assertTrue(all(row["id"].startswith("bob-erp-") for row in observed["records"]))
        server = adapter.read("session_events", "bob-erp-report", "bob", [observed], timeout=1, snapshot=snapshot)
        self.assertIn("identity=erp.bob", server["records"][0]["text"])
        self.assertNotIn("alice", json.dumps(server))

    def test_copied_foreign_session_reference_cannot_read_another_accounts_logs(self):
        account = self.telemetry["resources"]["erp"]["accounts"]["erp.bob"]
        account["client_events"][0]["session_ref"] = "session-731"  # Exists only for alice.
        self.save_events()
        adapter = self.adapter()
        snapshot = adapter.capture("bob")
        observation = adapter.read("client_events", "", "bob", [], timeout=1, snapshot=snapshot)
        with self.assertRaises(DiagnosticDataGap) as error:
            adapter.read("session_events", "session-731", "bob", [observation], timeout=1, snapshot=snapshot)
        self.assertEqual(error.exception.code, "session_records_missing")

    def test_record_owner_or_account_mismatch_is_rejected_before_model_use(self):
        for field, value in (("owner_id", "alice"), ("account_id", "alice")):
            original = deepcopy(self.telemetry)
            self.telemetry["resources"]["erp"]["accounts"]["erp.bob"]["client_events"][0][field] = value
            self.save_events()
            result = self.runner().run(text="ERP 503", owner="bob", binding="scope-test")
            self.assertEqual(result["data_availability"]["code"], "source_invalid")
            self.assertEqual(result["model_calls"], 0)
            self.telemetry = original

    def test_gap_taxonomy_hands_off_without_calling_the_model_or_asking_user_for_configuration(self):
        cases = ("account_unbound", "source_not_configured", "account_not_found", "no_records", "source_forbidden", "source_unavailable", "source_invalid")
        original = deepcopy(self.telemetry)
        original_bindings = self.path.read_bytes()
        for code in cases:
            self.telemetry = deepcopy(original)
            self.path.write_bytes(original_bindings)
            resource = self.telemetry["resources"]["erp"]
            if code == "account_unbound":
                self.remove_binding()
            elif code == "source_not_configured":
                self.telemetry["resources"].pop("erp")
            elif code == "account_not_found":
                resource["accounts"].pop("erp.bob")
            elif code == "no_records":
                resource["accounts"]["erp.bob"].update(client_events=[], sessions={})
            elif code == "source_invalid":
                self.telemetry = []
            else:
                resource["source_status"] = "forbidden" if code == "source_forbidden" else "unavailable"
            self.save_events()
            model = ScriptModel()  # Any model call would fail the scenario.
            result = self.runner(model=model).run(text="ERP 页面失败", owner="bob", binding="gap:" + code)
            self.assertEqual(result["data_availability"]["code"], code)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["model_calls"], 0)
            self.assertFalse(result["verified"])
            self.assertNotEqual(result["stop_reason"], "user_input")
            self.assertTrue(incident_attachment(result)["next_check"])

    def test_normal_fault_disabled_permission_and_empty_log_scenarios_are_covered(self):
        original = deepcopy(self.telemetry)
        for state in ("normal", "fault", "disabled", "permission_denied", "empty"):
            self.telemetry = deepcopy(original)
            account = self.telemetry["resources"]["erp"]["accounts"]["erp.bob"]
            if state == "empty":
                account.update(client_events=[], sessions={})
            else:
                line = {"normal": "account=erp.bob authenticated; response=200; operation completed",
                        "fault": "account=erp.bob authenticated; application request timed out; response=504",
                        "disabled": "account=erp.bob disabled; authentication rejected; no session allocated",
                        "permission_denied": "account=erp.bob authenticated; required_permission=reports.read missing; response=403"}[state]
                account["client_events"][0]["text"] = line
                if state == "disabled":
                    self.update_binding(enabled=False)
            self.save_events()
            result = self.runner().run(text="ERP 本次请求", owner="bob", binding=state)
            self.assertFalse(result["verified"])
            self.assertEqual(result["assessment"], "insufficient")
            self.assertEqual(result["model_calls"], 0 if state == "empty" else 1)
            self.assertTrue(all(obs["owner"] == "bob" for obs in result["observations"]))
            self.update_binding(enabled=True)

    def test_mapping_revocation_after_model_call_discards_the_model_result(self):
        directory = self
        class Model:
            source = "test"
            def choose(self, *args, **kwargs):
                directory.update_binding(account_id="erp.bob.reassigned")
                return directory.insufficient(), {}
        result = self.runner(model=Model()).run(text="ERP 503", owner="bob", binding="race")
        self.assertEqual(result["data_availability"]["code"], "account_binding_changed")
        self.assertEqual(result["citations"], [])
        self.assertFalse(result["evidence_verified"])

    def test_mapping_change_while_investigation_is_paused_cannot_resume_against_a_new_account(self):
        adapter = self.adapter()
        result = self.runner(adapter).run(text="ERP 503", owner="bob", binding="pause")
        result.update(status="running", pending_tool=None)
        self.update_binding(account_id="erp.bob.new")
        resumed = self.runner(adapter, ScriptModel()).run(text="ERP 503", owner="bob", binding="pause", saved=result)
        self.assertEqual(resumed["data_availability"]["code"], "account_binding_changed")
        self.assertTrue(all(obs.get("account_id") == "erp.bob" for obs in resumed["observations"]))

    def test_legacy_vpn_read_tools_use_same_explicit_mapping(self):
        adapter = LocalVPNDiagnostics(ROOT / "data/diagnostics/vpn.json", accounts=self.directory)
        result = DiagnosticRunner(adapter, RuleDiagnosticModel(), mode="rule").run(text="VPN 拒绝连接", owner="bob", binding="vpn")
        self.assertEqual(result["finding"], "access_missing")
        self.assertEqual(result["account_scope"]["account_id"], "vpn.bob")
        self.assertTrue(all(obs["owner"] == "bob" for obs in result["observations"]))
        self.remove_binding("bob", "vpn")
        result = DiagnosticRunner(adapter, RuleDiagnosticModel(), mode="rule").run(text="VPN 拒绝连接", owner="bob", binding="missing")
        self.assertEqual(result["data_availability"]["code"], "account_unbound")
        self.assertEqual(result["model_calls"], 0)

    def test_failed_mapping_does_not_block_incident_acceptance_and_generates_support_work(self):
        self.remove_binding()
        service, agent = self.modern_service()
        state = self.create_incident(service, agent)
        self.assertEqual(state.action_result.status, "succeeded")
        with patch("app.investigation.InvestigationModel.choose", side_effect=AssertionError("must not call model")):
            done = service.investigate(self.bob, state.ticket.id)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.diagnostic_report["data_availability"]["code"], "account_unbound")
        self.assertFalse((done.clarification or {}).get("need_clarify"))
        self.assertEqual(done.diagnosis["model_calls"], 0)
        user = present_ticket(done.to_dict())
        self.assertIn("尚未关联", user["data_status"]["message"])
        self.assertNotIn("account_unbound", json.dumps(user))
        support = self.env.portal.__class__(service).get(self.env.manager, done.ticket.id, "support")["support"]
        self.assertEqual(support["data_issue"]["code"], "account_unbound")
        self.assertTrue(support["can_claim"])

    def test_model_investigation_and_attachment_are_scoped_to_bobs_actual_erp_account(self):
        service, agent = self.modern_service()
        state = self.create_incident(service, agent)
        with patch("app.investigation.InvestigationModel.choose", return_value=(self.insufficient(), {})):
            done = service.investigate(self.bob, state.ticket.id)
        report = done.ticket.diagnostic_report
        self.assertEqual(report["account_scope"]["account_id"], "erp.bob")
        self.assertEqual(done.ticket.owner_id, "bob")
        self.assertTrue(report["observations"])
        self.assertNotIn("alice", json.dumps(report))

    def test_sensitive_write_targets_mapped_account_and_rebinding_invalidates_approval(self):
        service, agent = self.env.service, self.env.agent
        first = service.create(self.bob, title="ERP 权限申请", description="申请 ERP 普通使用权限")
        self.assertEqual(first.approval.account_binding["account_id"], "erp.bob")
        rows = present_ticket(first.to_dict())["request_details"]
        self.assertIn("erp.bob", [row["value"] for row in rows])
        self.update_binding(account_id="erp.bob.new")
        with self.assertRaises(IdempotencyConflict):
            service.approve(self.env.manager, first.approval.id, True)
        self.assertFalse(self.env.actions.registrations)
        self.assertEqual(self.env.audit.get_state(first.ticket.id)["approval"]["status"], "invalidated")

    def test_disabled_business_account_can_be_investigated_but_cannot_be_granted_or_reset(self):
        self.update_binding(enabled=False)
        result = self.runner().run(text="ERP 账号停用", owner="bob", binding="disabled")
        self.assertEqual(result["evidence_snapshot"]["account_scope"]["code"], "business_account_disabled")
        self.assertEqual(result["model_calls"], 1)
        blocked = self.env.service.create(self.bob, title="ERP 权限", description="申请 ERP 普通使用权限")
        self.assertEqual(blocked.ticket.status, "manual_review")
        self.assertIsNone(blocked.approval)
        self.assertFalse(self.env.actions.registrations)
        self.update_binding(resource="self_account", enabled=False)
        password = self.env.service.create(self.bob, title="密码重置", description="忘记密码")
        self.assertEqual(password.ticket.status, "manual_review")
        self.assertFalse(self.env.actions.registrations)

    def test_fixed_mapping_allows_fresh_approval_without_overwriting_old_request_target(self):
        self.remove_binding()
        first = self.env.service.create(self.bob, title="ERP 权限", description="申请 ERP 普通使用权限", idempotency_key="missing")
        duplicate = self.env.service.create(self.bob, title="ERP 权限", description="申请 ERP 普通使用权限", idempotency_key="retry-before-fix")
        self.assertEqual(first.ticket.id, duplicate.ticket.id)
        self.path.write_text(json.dumps(demo_bindings()), encoding="utf-8")
        fresh = self.env.service.create(self.bob, title="ERP 权限", description="申请 ERP 普通使用权限", idempotency_key="after-fix")
        self.assertNotEqual(first.ticket.id, fresh.ticket.id)
        self.assertEqual(fresh.ticket.status, "awaiting_approval")
        self.assertEqual(self.env.audit.get_state(first.ticket.id)["ticket"]["account_binding"]["code"], "account_unbound")

    def test_own_business_alias_is_accepted_but_another_users_account_is_not(self):
        own = self.env.service.create(self.bob, title="ERP 故障", description="我的 ERP 账号是 erp.bob，页面报错 503。")
        self.assertEqual(own.ticket.recommended_action, "open_incident")
        self.assertEqual(own.ticket.account_binding["account_id"], "erp.bob")
        other = self.env.service.create(self.bob, title="ERP 故障", description="ERP 账号是 alice，页面报错 503。")
        self.assertEqual(other.ticket.status, "manual_review")
        self.assertEqual(len(self.env.actions.registrations), 1)

    def test_client_cannot_supply_account_binding_and_login_does_not_disclose_account_catalog(self):
        with TestClient(create_app(self.env.agent, identities=self.env.identities)) as client:
            headers = auth_headers(self.env.keys, "bob")
            self.assertNotIn("erp.bob", client.get("/me", headers=headers).text)
            for field in ("account_binding", "account_aliases", "account_context"):
                result = client.post("/tickets", headers=headers, json={"title": "ERP 故障", "description": "ERP 503", field: {"account_id": "alice"}})
                self.assertEqual(result.status_code, 422)
            own = self.env.service.create(self.bob, title="ERP 故障", description="ERP 页面报错 503")
            internal = client.get(f"/tickets/{own.ticket.id}/state", headers=auth_headers(self.env.keys, "manager"))
            self.assertEqual(internal.status_code, 200)
            self.assertNotIn("account_aliases", internal.json()["ticket"])
            self.assertNotIn("account_context", internal.json()["ticket"])
            self.assertNotIn("report.bob", internal.text)

    def test_rebinding_immediately_before_adapter_call_is_a_known_rejection(self):
        first = self.env.service.create(self.bob, title="ERP 权限", description="申请 ERP 普通使用权限")
        original = self.env.agent.tools.execute_action
        def race(ticket, action, **kwargs):
            self.update_binding(account_id="erp.bob.new")
            return original(ticket, action, **kwargs)
        with patch.object(self.env.agent.tools, "execute_action", side_effect=race):
            done = self.env.service.approve(self.env.manager, first.approval.id, True)
        self.assertEqual(done.action_result.status, "rejected")
        self.assertEqual(done.approval.status, "invalidated")
        self.assertFalse(self.env.actions.registrations)
        self.assertFalse(self.env.audit.pending_operations(first.ticket.id))

    def test_http_registration_carries_frozen_business_account_without_changing_reporter(self):
        remote = http_fixtures.HTTPActionTests("test_concurrent_same_key_only_creates_one_record")
        remote.setUp()
        try:
            from app.http_actions import IncidentHTTPAdapter
            with remote.server() as base:
                self.env.agent.tools.adapter = IncidentHTTPAdapter(base, remote.key)
                state = self.env.service.create(self.bob, title="ERP 故障", description="ERP 报错 503")
                with closing(sqlite3.connect(remote.root / "remote.db")) as db:
                    raw = json.loads(db.execute("SELECT payload FROM incidents WHERE ticket_id=?", (state.ticket.id,)).fetchone()[0])["payload"]
                self.assertEqual(raw["owner"], "bob")
                self.assertEqual(raw["business_account"]["account_id"], "erp.bob")
                self.assertEqual(raw["business_account"]["owner_id"], "bob")
                self.assertEqual(raw["business_account"]["resource"], "erp")
                self.assertEqual(remote.count("incidents"), 1)
        finally:
            remote.tearDown()


if __name__ == "__main__":
    unittest.main()
