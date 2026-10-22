import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.knowledge import KnowledgeBase
from app.models import ActionResult, Ticket
from app.model_adapter import ResilientDecisionModel
from app.reliability import CircuitBreaker, CircuitOpenError, IdempotencyConflict, OperationPending
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AuditStore(self.root / 'test.db')
        self.kb = KnowledgeBase(self.root / 'kb', backend='lexical')
        self.agent = EnterpriseAgent(EnterpriseTools(self.kb, self.store), self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def ticket(self, title='密码重置', key='same'):
        return Ticket(title, '无法登录', 'alice', 'IT', idempotency_key=key)

    def test_replay_survives_restart_and_rejects_changed_payload(self):
        first = self.agent.run(self.ticket())
        restarted = EnterpriseAgent(EnterpriseTools(self.kb, AuditStore(self.store.db_path)), self.store)
        replay = restarted.run(self.ticket())
        self.assertEqual(first.to_dict(), replay.to_dict())
        self.assertEqual([event['node'] for event in self.store.list_events(first.ticket.id)],
                         ['intake', 'clarify', 'retrieve', 'plan', 'authorize', 'execute', 'finalize'])
        with self.assertRaises(IdempotencyConflict):
            restarted.run(self.ticket('different'))

    def test_concurrent_claim_and_uncertain_operation_are_not_reexecuted(self):
        started, release = threading.Event(), threading.Event()
        results = []
        def work():
            started.set()
            release.wait(5)
            return {'ok': True}
        def worker():
            results.append(self.store.once('concurrent', {}, work))
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(3))
        try:
            with self.assertRaises(OperationPending):
                AuditStore(self.store.db_path).once('concurrent', {}, lambda: self.fail('duplicate'))
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(results, [{'ok': True}])
        with self.assertRaises(RuntimeError):
            self.store.once('uncertain', {}, lambda: (_ for _ in ()).throw(RuntimeError()))
        with self.assertRaises(OperationPending):
            self.store.once('uncertain', {}, lambda: self.fail('must not retry'))

    def test_duplicate_approval_and_conflicting_decision(self):
        state = self.agent.run(Ticket('申请报表库权限', '需要报表数据库只读权限', 'alice', 'IT'))
        approval_id = state.approval.id
        restarted = EnterpriseAgent(self.agent.tools, self.store)
        first = restarted.approve(approval_id, 'manager', True)
        second = self.agent.approve(approval_id, 'manager', True)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.approval.status, 'approved')
        self.assertTrue(first.decision)
        self.assertEqual([x['node'] for x in first.trace][-3:], ['approval', 'execute', 'finalize'])
        with self.assertRaises(IdempotencyConflict):
            restarted.approve(approval_id, 'manager', False)

    def test_tool_idempotency_and_failure_degradation(self):
        ticket = self.ticket()
        def result(_ticket, action, *, idempotency_key):
            return ActionResult(action, 'succeeded', 'done', idempotency_key)
        with patch.object(self.agent.tools, '_execute_action', side_effect=result) as execute:
            self.assertEqual(self.agent.tools.execute_action(ticket, 'reset_password').message, 'done')
            self.assertEqual(self.agent.tools.execute_action(ticket, 'reset_password').message, 'done')
            self.assertEqual(execute.call_count, 1)

    def test_client_idempotency_key_is_forwarded_to_adapter(self):
        seen = []
        class Adapter:
            def execute(self, ticket, action, *, idempotency_key):
                seen.append(idempotency_key)
                return ActionResult(action, 'succeeded', 'done', idempotency_key)
        agent = EnterpriseAgent(self.agent.tools, self.store)
        agent.tools.adapter = Adapter()
        ticket = Ticket('密码', '登录失败', 'alice', 'IT', idempotency_key='client-key-001')
        result = agent.tools.execute_action(ticket, 'reset_password')
        self.assertEqual(seen, ['client-key-001'])
        self.assertEqual(result.idempotency_key, 'client-key-001')
        with patch.object(self.agent.tools, '_execute_action', side_effect=TimeoutError):
            state = self.agent.run(self.ticket(key='failure'))
        self.assertEqual(state.ticket.status, 'manual_review')
        self.assertEqual(state.degradation[-1]['component'], 'tool')

    def test_half_open_allows_only_one_probe(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10)
        with patch('app.reliability.time.monotonic', return_value=0):
            with self.assertRaises(ValueError):
                breaker.call(lambda: (_ for _ in ()).throw(ValueError()))
        with patch('app.reliability.time.monotonic', return_value=1):
            with self.assertRaises(CircuitOpenError):
                breaker.call(lambda: self.fail('open'))
        with patch('app.reliability.time.monotonic', return_value=11):
            def probe():
                with self.assertRaises(CircuitOpenError):
                    breaker.call(lambda: self.fail('duplicate probe'))
                return 'recovered'
            self.assertEqual(breaker.call(probe), 'recovered')
        self.assertEqual(breaker.state, 'closed')

    def test_llm_circuit_short_circuits_then_falls_back(self):
        class Broken:
            def __init__(self):
                self.calls = 0
            def decide(self, *args):
                self.calls += 1
                raise TimeoutError()
        primary = Broken()
        model = ResilientDecisionModel(primary)
        for _ in range(4):
            result = model.decide('密码', '无法登录')
        self.assertEqual(primary.calls, 3)
        self.assertEqual(result.metadata['fallback_reason'], 'CircuitOpenError')

    def test_fixed_policy_does_not_depend_on_vector_availability(self):
        self.kb.backend = 'chroma'
        with patch.object(self.kb, '_vector_search', side_effect=RuntimeError) as vector:
            state = self.agent.run(self.ticket())
        vector.assert_not_called()
        self.assertEqual(state.ticket.status, 'closed')

    def test_fastapi_validation_replay_and_conflict(self):
        payload = dict(title='密码重置', description='无法登录')
        identities, keys = demo_identities(self.root)
        with TestClient(create_app(self.agent, identities=identities), headers=auth_headers(keys)) as client:
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/openapi.json').status_code, 200)
            first = client.post('/tickets', json=payload, headers={'Idempotency-Key': 'api'})
            second = client.post('/tickets', json=payload, headers={'Idempotency-Key': 'api'})
            self.assertEqual(first.status_code, 201)
            self.assertEqual(first.json(), second.json())
            self.assertEqual(client.get('/tickets/' + first.json()['ticket']['id']).status_code, 200)
            self.assertEqual(client.post('/tickets', json={**payload, 'title': 'changed'},
                headers={'Idempotency-Key': 'api'}).status_code, 409)
            self.assertEqual(client.post('/tickets', json={}).status_code, 422)
            self.assertEqual(client.post('/approvals/unknown', json={'approved': 'false'}).status_code, 422)
            self.assertEqual(client.get('/tickets/unknown').status_code, 404)


if __name__ == '__main__':
    unittest.main()
