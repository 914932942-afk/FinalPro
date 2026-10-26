"""Outbound HTTP crosses a process boundary and commits to a separate SQLite database."""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx

from app.audit import AuditStore
from app.demo_downstream import IncidentStore
from app.errors import ActionRejectedError, IdempotencyConflict, OperationPending, ToolTimeoutError, ToolUnavailableError, ToolResultError
from app.http_actions import IncidentHTTPAdapter
from app.knowledge import KnowledgeBase
from app.models import Ticket
from app.config import get_settings
from app.auth import Principal
from app.service import TicketService
from app.workflow import EnterpriseAgent
from app.tools import EnterpriseTools

ROOT=Path(__file__).resolve().parents[1]


class HTTPActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.key=secrets.token_urlsafe(32)
        self.ticket=Ticket("VPN 故障","当前 VPN 连接超时","alice","IT",owner_id="alice")
        self.ticket.execution_plan={"resource":"vpn"}

    def tearDown(self):self.temp.cleanup()

    @contextmanager
    def server(self, *options):
        with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        env=dict(os.environ,DEMO_DOWNSTREAM_KEY=self.key)
        process=subprocess.Popen([sys.executable,"-m","app.demo_downstream","--db",str(self.root/"remote.db"),"--port",str(port),*options],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base=f"http://127.0.0.1:{port}"
        try:
            ready=False
            for _ in range(100):
                try:
                    if httpx.get(base+"/health",timeout=.2,trust_env=False).status_code==200:ready=True;break
                except httpx.HTTPError:pass
                if process.poll() is not None:break
                time.sleep(.05)
            self.assertTrue(ready,"independent downstream failed to start")
            yield base
        finally:
            process.terminate();process.wait(timeout=10)

    def count(self, table):
        with closing(sqlite3.connect(self.root/"remote.db")) as db:return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def tools(self, adapter):
        audit=AuditStore(self.root/"agent.db")
        return EnterpriseTools(KnowledgeBase(self.root/"kb",backend="lexical"),audit,adapter),audit

    def test_persistent_receipt_replay_conflict_and_separate_attachment(self):
        with self.server() as base:
            adapter=IncidentHTTPAdapter(base,self.key)
            first=adapter.execute(self.ticket,"open_incident",idempotency_key="register")
            self.assertTrue(first.simulated)
            self.assertEqual(first.downstream_id,adapter.execute(self.ticket,"open_incident",idempotency_key="register").downstream_id)
            self.ticket.description="different"
            with self.assertRaises(IdempotencyConflict):adapter.execute(self.ticket,"open_incident",idempotency_key="register")
            self.ticket.diagnostic_report={"assessment":"insufficient","summary":"需核查"}
            adapter.attach_evidence(self.ticket,idempotency_key="attachment")
            adapter.attach_evidence(self.ticket,idempotency_key="attachment")
            self.assertEqual(self.count("incidents"),1);self.assertEqual(self.count("attachments"),1)
        with self.server() as base:
            receipt=IncidentHTTPAdapter(base,self.key).lookup("register")
            self.assertEqual(receipt["downstream_id"],first.downstream_id)

    def test_write_committed_response_times_out_no_replay_even_after_agent_restart(self):
        with self.server("--test-after-commit-delay","0.8") as base:
            adapter=IncidentHTTPAdapter(base,self.key,timeout=.15)
            tools,audit=self.tools(adapter)
            with self.assertRaises(ToolTimeoutError):tools.execute_action(self.ticket,"open_incident")
            self.assertEqual(self.count("incidents"),1)
            restarted,_=self.tools(IncidentHTTPAdapter(base,self.key))
            with self.assertRaises(OperationPending):restarted.execute_action(self.ticket,"open_incident")
            receipt=adapter.lookup(tools.action_key(self.ticket,"open_incident"))
            self.assertEqual(receipt["status"],"succeeded")
            self.assertEqual(audit.get_operation(tools.action_key(self.ticket,"open_incident"))["status"],"pending")
            self.assertEqual(self.count("incidents"),1)

    def test_attachment_outage_preserves_registration_and_pending_ledger(self):
        with self.server("--test-reject-attachments") as base:
            tools,audit=self.tools(IncidentHTTPAdapter(base,self.key))
            tools.execute_action(self.ticket,"open_incident")
            self.ticket.diagnostic_report={"assessment":"insufficient"}
            with self.assertRaises(ToolUnavailableError):tools.attach_evidence(self.ticket)
            with self.assertRaises(OperationPending):tools.attach_evidence(self.ticket)
            self.assertEqual(self.count("incidents"),1);self.assertEqual(self.count("attachments"),0)

    def test_service_auth_and_no_silent_simulation_for_failed_incidents(self):
        with self.server() as base:
            self.assertEqual(httpx.get(base+"/ready",trust_env=False).status_code,401)
            adapter=IncidentHTTPAdapter(base,"wrong"*8)
            with self.assertRaises(ActionRejectedError):adapter.execute(self.ticket,"open_incident",idempotency_key="one")
            self.assertEqual(self.count("incidents"),0)

    def test_invalid_receipt_never_counts_as_success(self):
        adapter=IncidentHTTPAdapter("http://127.0.0.1:1",self.key)
        with patch.object(adapter,"_request",return_value={"status":"succeeded"}),self.assertRaises(ToolResultError):
            adapter.execute(self.ticket,"open_incident",idempotency_key="one")
        # Password and permission changes explicitly remain in-process simulations.
        self.assertTrue(adapter.execute(self.ticket,"reset_password",idempotency_key="reset").simulated)

    def test_concurrent_same_key_only_creates_one_record(self):
        from concurrent.futures import ThreadPoolExecutor
        store=IncidentStore(self.root/"remote.db")
        body={"ticket_id":"T-one","title":"test"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda _:store.write("open_incident","one",body),range(4)))
        self.assertTrue(all(x==values[0] for x in values))
        self.assertEqual(self.count("incidents"),1)

    def test_clarification_reaches_the_separate_http_service_without_duplicate_registration(self):
        with self.server() as base:
            tools,audit=self.tools(IncidentHTTPAdapter(base,self.key))
            cfg=get_settings(_env_file=None,data_dir=self.root,db_path=audit.db_path,llm_mode="offline",
                rag_backend="lexical",diagnosis_mode="off",diagnosis_background=False)
            agent=EnterpriseAgent(tools,audit,settings=cfg)
            service=TicketService(agent)
            owner=Principal(user_id="alice",role="employee",department="IT")
            state=service.create(owner,title="系统打不开",description="请帮我看看")
            self.assertEqual(self.count("incidents"),0)
            done=service.clarify(owner,state.ticket.id,"ERP 登录后报错 503",state.clarification_round)
            with closing(sqlite3.connect(self.root/"remote.db")) as db:
                payload=json.loads(db.execute("SELECT payload FROM incidents WHERE ticket_id=?",(state.ticket.id,)).fetchone()[0])["payload"]
            self.assertEqual(payload["description"],"请帮我看看")
            self.assertEqual(payload["registration_context"],done.ticket.registration_context)
            self.assertIn("ERP 登录后报错 503",payload["registration_context"]["effective_description"])
            replay=service.clarify(owner,state.ticket.id,"ERP 登录后报错 503",state.clarification_round)
            self.assertEqual(replay.action_result.to_dict(),done.action_result.to_dict())
            self.assertEqual(self.count("incidents"),1)

    def test_frozen_registration_survives_response_loss_without_a_second_http_write(self):
        with self.server("--test-after-commit-delay","0.8") as base:
            tools,audit=self.tools(IncidentHTTPAdapter(base,self.key,timeout=.15))
            cfg=get_settings(_env_file=None,data_dir=self.root,db_path=audit.db_path,llm_mode="offline",
                rag_backend="lexical",diagnosis_mode="off",diagnosis_background=False)
            agent=EnterpriseAgent(tools,audit,settings=cfg)
            service=TicketService(agent)
            owner=Principal(user_id="alice",role="employee",department="IT")
            waiting=service.create(owner,title="系统打不开",description="请帮我看看")
            unknown=service.clarify(owner,waiting.ticket.id,"ERP 登录后报错 503",waiting.clarification_round)
            self.assertEqual(unknown.action_result.status,"unknown")
            with closing(sqlite3.connect(self.root/"remote.db")) as db:
                body=json.loads(db.execute("SELECT payload FROM incidents WHERE ticket_id=?",(waiting.ticket.id,)).fetchone()[0])["payload"]
            self.assertEqual(body["registration_context"],unknown.ticket.registration_context)
            restarted=EnterpriseAgent(tools,AuditStore(audit.db_path),settings=cfg)
            manager=Principal(user_id="manager",role="manager",department="IT",support_groups=["network","applications","data","service_desk"])
            recovered=TicketService(restarted).recover(manager,waiting.ticket.id)
            self.assertEqual(recovered.action_result.status,"unknown")
            self.assertEqual(recovered.ticket.registration_context,unknown.ticket.registration_context)
            self.assertEqual(self.count("incidents"),1)
