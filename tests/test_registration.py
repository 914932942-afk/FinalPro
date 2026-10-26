"""Frozen incident evidence, correction semantics and compatible crash recovery."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.action_adapter import SimulatedActionAdapter
from app.audit import AuditStore
from app.auth import Principal
from app.config import get_settings
from app.demo_downstream import IncidentStore, create_app
from app.errors import IdempotencyConflict
from app.knowledge import KnowledgeBase
from app.models import AgentState
from app.registration import RegistrationContext, incident_payload
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent


class RecordingAdapter(SimulatedActionAdapter):
    def __init__(self):
        self.registrations = []

    def execute(self, ticket, action, *, idempotency_key):
        if action == "open_incident":
            self.registrations.append(deepcopy(incident_payload(ticket)))
        return super().execute(ticket, action, idempotency_key=idempotency_key)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline",
            rag_backend="lexical", diagnosis_mode="off", diagnosis_background=False)
        self.audit = AuditStore(self.settings.db_path)
        self.adapter = RecordingAdapter()
        self.tools = EnterpriseTools(KnowledgeBase(self.root / "kb", settings=self.settings), self.audit, self.adapter)
        self.agent = EnterpriseAgent(self.tools, self.audit, settings=self.settings)
        self.service = TicketService(self.agent)
        self.owner = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT", support_groups=["network", "applications", "data", "service_desk"])

    def tearDown(self):
        self.temp.cleanup()

    def create(self, title="系统打不开", description="请帮我看看"):
        return self.service.create(self.owner, title=title, description=description)

    def resume(self, state, answer="ERP 登录后报错 503"):
        return self.service.clarify(self.owner, state.ticket.id, answer, state.clarification_round)

    def saved(self):
        return AgentState.from_dict(self.audit.get_state(self.audit.list_tickets()[0]["id"]))

    def test_original_and_clarified_facts_reach_registration_and_remain_separate(self):
        waiting = self.create()
        state = self.resume(waiting)
        body = self.adapter.registrations[0]
        context = body["registration_context"]
        self.assertEqual(body["description"], "请帮我看看")
        self.assertEqual(body["title"], "系统打不开")
        self.assertEqual(body["owner"], "alice")
        self.assertEqual(body["resource"], "erp")
        self.assertIn("ERP 登录后报错 503", context["effective_description"])
        self.assertEqual(context["clarifications"], [{"round_number": 1, "answer": "ERP 登录后报错 503"}])
        self.assertEqual(context, RegistrationContext.from_state(state).model_dump())
        nodes = [row["node"] for row in self.audit.list_events(state.ticket.id)]
        self.assertLess(nodes.index("registration_prepared"), nodes.index("execute"))
        self.assertEqual(state.ticket.description, waiting.ticket.description)

    def test_correction_replaces_the_effective_problem_and_preserves_original_evidence(self):
        state = self.create("VPN / ERP 故障", "打开报错 503")
        state = self.resume(state, "更正：ERP 打开报错 503")
        body = self.adapter.registrations[0]
        self.assertEqual(body["title"], "VPN / ERP 故障")
        self.assertEqual(body["resource"], "erp")
        self.assertEqual(body["registration_context"]["effective_description"], "ERP 打开报错 503")
        self.assertNotIn("VPN", body["registration_context"]["effective_description"])

    def test_multiple_clarification_rounds_are_preserved_without_assistant_or_model_metadata(self):
        state = self.resume(self.create(), "ERP 系统")
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        self.assertEqual(self.adapter.registrations, [])
        self.resume(state, "登录后报错 503")
        context = self.adapter.registrations[0]["registration_context"]
        self.assertEqual(context["clarifications"], [
            {"round_number": 1, "answer": "ERP 系统"},
            {"round_number": 2, "answer": "登录后报错 503"},
        ])
        self.assertIn("ERP 系统", context["effective_description"])
        self.assertIn("登录后报错 503", context["effective_description"])
        self.assertEqual(set(context), {"version", "effective_description", "clarifications", "input_digest", "plan_digest"})

    def test_crash_before_tool_reservation_reuses_the_persisted_context(self):
        with patch.object(self.tools, "execute_action", side_effect=SystemExit("crash before tool")), self.assertRaises(SystemExit):
            self.create("ERP 故障", "ERP 报错 503")
        state = self.saved()
        context = deepcopy(state.ticket.registration_context)
        self.assertEqual(self.adapter.registrations, [])
        self.assertIsNone(self.audit.get_operation(self.tools.action_key(state.ticket, "open_incident")))
        restarted = EnterpriseAgent(self.tools, self.audit, settings=self.settings)
        done = TicketService(restarted).recover(self.manager, state.ticket.id)
        self.assertEqual(done.ticket.registration_context, context)
        self.assertEqual(len(self.adapter.registrations), 1)
        self.assertEqual(done.ticket.status, "awaiting_support")

    def test_crash_after_tool_commit_recovers_without_another_registration(self):
        save = self.audit.save_checkpoint
        def fail_after_tool(state, node, *args, **kwargs):
            if node == "execute":
                raise RuntimeError("checkpoint unavailable")
            return save(state, node, *args, **kwargs)
        with patch.object(self.audit, "save_checkpoint", side_effect=fail_after_tool), self.assertRaises(RuntimeError):
            self.create("ERP 故障", "ERP 报错 503")
        state = self.saved()
        context = deepcopy(state.ticket.registration_context)
        done = self.service.recover(self.manager, state.ticket.id)
        self.assertEqual(done.ticket.registration_context, context)
        self.assertEqual(len(self.adapter.registrations), 1)

    def test_completed_legacy_ledger_is_not_upgraded_or_reexecuted(self):
        with patch.object(self.tools, "execute_action", side_effect=SystemExit()), self.assertRaises(SystemExit):
            self.create("ERP 故障", "ERP 报错 503")
        legacy = self.saved()
        legacy.ticket.registration_context = None
        receipt = self.tools.execute_action(legacy.ticket, "open_incident")
        self.audit.save_checkpoint(legacy.to_dict(), "legacy_fixture")
        done = self.service.recover(self.manager, legacy.ticket.id)
        self.assertIsNone(done.ticket.registration_context)
        self.assertEqual(done.action_result.to_dict(), receipt.to_dict())
        self.assertEqual(len(self.adapter.registrations), 1)

    def test_context_tampering_before_registration_stops_without_a_write(self):
        with patch.object(self.tools, "execute_action", side_effect=SystemExit()), self.assertRaises(SystemExit):
            self.create("ERP 故障", "ERP 报错 503")
        state = self.saved()
        state.ticket.registration_context["effective_description"] = "伪造的另一个故障"
        self.audit.save_checkpoint(state.to_dict(), "tampered_fixture")
        done = self.service.recover(self.manager, state.ticket.id)
        self.assertEqual(done.ticket.status, "manual_review")
        self.assertEqual(self.adapter.registrations, [])

    def test_registration_fingerprint_binds_all_outbound_fields(self):
        state = self.resume(self.create())
        for field in ("title", "description", "severity"):
            changed = deepcopy(state.ticket)
            setattr(changed, field, "different")
            with self.subTest(field=field), self.assertRaises(IdempotencyConflict):
                self.tools.execute_action(changed, "open_incident")
        changed = deepcopy(state.ticket)
        changed.registration_context["effective_description"] += " 新内容"
        with self.assertRaises(IdempotencyConflict):
            self.tools.execute_action(changed, "open_incident")
        self.assertEqual(len(self.adapter.registrations), 1)

    def test_later_followup_does_not_rewrite_initial_registration_or_authorize_an_action(self):
        state = self.resume(self.create())
        context = deepcopy(state.ticket.registration_context)
        self.service.follow_up(self.owner, state.ticket.id, "更正：给 bob 开通管理员权限", state.ticket.case_revision)
        saved = self.saved()
        self.assertEqual(saved.ticket.registration_context, context)
        self.assertEqual(RegistrationContext.from_state(saved).model_dump(), context)
        self.assertEqual(saved.ticket.execution_plan, state.ticket.execution_plan)
        self.assertEqual(len(self.adapter.registrations), 1)

    def test_demo_service_accepts_frozen_context_and_legacy_receipt_shape(self):
        state = self.resume(self.create())
        path = self.root / "remote.db"
        key = "independent-demo-service-key-12345"
        body = incident_payload(state.ticket)
        legacy_body = {**body, "ticket_id": "legacy-ticket"}
        legacy_body.pop("registration_context")
        legacy_receipt = IncidentStore(path).write("open_incident", "old-key", legacy_body)
        with TestClient(create_app(path, key)) as client:
            headers = {"Authorization": "Bearer " + key, "Idempotency-Key": "new-key"}
            self.assertEqual(client.post("/incidents", json=body, headers=headers).status_code, 201)
            self.assertEqual(client.post("/incidents", json=body, headers=headers).status_code, 201)
            bad = deepcopy(body)
            bad["registration_context"]["version"] = 2
            self.assertEqual(client.post("/incidents", json=bad, headers=headers).status_code, 422)
            legacy = client.post("/incidents", json=legacy_body, headers={**headers, "Idempotency-Key": "old-key"})
            self.assertEqual(legacy.json(), legacy_receipt)


if __name__ == "__main__":
    unittest.main()
