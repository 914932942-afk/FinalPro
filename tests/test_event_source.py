import json
from pathlib import Path
import tempfile
import unittest

import httpx

from app.business_accounts import AccountDirectory, demo_bindings
from app.event_source import HTTPEventSource
from app.event_query import IncidentWindow
from app.business_accounts import DiagnosticDataGap


class EventSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mapping = self.root / "bindings.json"
        self.mapping.write_text(json.dumps(demo_bindings()), encoding="utf-8")
        self.scope = AccountDirectory(self.mapping).resolve("alice", "vpn")
        self.window = IncidentWindow("2026-10-27T02:15:00+00:00", "2026-10-27T02:45:00+00:00", "user_explicit_time")

    def tearDown(self):
        self.temp.cleanup()

    def source(self, page):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/events/query")
            self.assertEqual(request.headers["authorization"], "Bearer " + "k" * 32)
            return httpx.Response(200, json=page)
        return HTTPEventSource("http://telemetry.test", "k" * 32, transport=httpx.MockTransport(handler))

    def page(self, *, foreign=False, changed_snapshot=False):
        account = {"owner_id": "alice", "client_events": [{"id": "c1", "text": "current", "owner_id": "alice",
            "account_id": "alice", "session_ref": "s1", "event_at": "2026-10-27T02:30:00+00:00"}],
            "sessions": {"s1": [{"id": "g1", "text": "server", "owner_id": "alice", "account_id": "alice",
                "event_at": "2026-10-27T02:30:01+00:00"}]}}
        if foreign:
            account["sessions"]["s1"][0]["owner_id"] = "bob"
        return {"schema_version": 1, "owner_id": "alice", "resource": "vpn", "account_id": "alice",
                "start_at": self.window.start_at, "end_at": self.window.end_at, "snapshot_id": "snap-1" if not changed_snapshot else "snap-2",
                "source_id": "test-source", "timezone": "+00:00", "simulated": False, "truncated": False,
                "account": account, "next_cursor": None}

    def test_scope_and_snapshot_provenance_are_returned(self):
        result = self.source(self.page()).capture(self.scope, self.window.to_dict(), timeout=2)
        self.assertEqual(result[0]["owner_id"], "alice")
        self.assertEqual(result[0]["client_events"][0]["id"], "c1")
        self.assertEqual(result[1]["source_id"], "test-source")
        self.assertFalse(result[1]["simulated"])

    def test_foreign_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_invalid|归属"):
            self.source(self.page(foreign=True)).capture(self.scope, self.window.to_dict(), timeout=2)

    def test_snapshot_change_between_pages_is_rejected(self):
        pages = [self.page(), self.page(changed_snapshot=True)]
        def handler(request):
            return httpx.Response(200, json=pages.pop(0))
        source = HTTPEventSource("http://telemetry.test", "k" * 32, max_pages=2, transport=httpx.MockTransport(handler))
        # One page is enough to complete; a continuation is required to expose
        # the snapshot fence, so make the first response advertise one.
        first = self.page(); first["next_cursor"] = "next"
        pages[:] = [first, self.page(changed_snapshot=True)]
        with self.assertRaises(DiagnosticDataGap):
            source.capture(self.scope, self.window.to_dict(), timeout=2)


if __name__ == "__main__":
    unittest.main()
