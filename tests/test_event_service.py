import unittest

from fastapi.testclient import TestClient

from app.event_service import create_event_app


class EventServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = "r" * 40
        cls.client = TestClient(
            create_event_app(
                "data/diagnostics/it_events.json",
                "data/diagnostics/account_bindings.example.json",
                cls.key,
                source_timezone="+08:00",
            )
        )
        cls.query = {
            "owner_id": "alice",
            "resource": "vpn",
            "account_id": "alice",
            "start_at": "2026-09-29T02:15:00+00:00",
            "end_at": "2026-09-29T02:45:00+00:00",
            "page_size": 1,
        }

    def test_read_only_scope_and_snapshot_pagination(self):
        response = self.client.post(
            "/v1/events/query",
            headers={"Authorization": "Bearer " + self.key},
            json=self.query,
        )
        self.assertEqual(response.status_code, 200)
        first = response.json()
        self.assertTrue(first["simulated"])
        self.assertEqual(first["source_id"], "reference-simulated-event-service")
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["account"]["owner_id"], "alice")
        self.assertTrue(first["account"]["client_events"] or first["account"]["sessions"])

        continuation = {**self.query, "cursor": first["next_cursor"], "snapshot_id": first["snapshot_id"]}
        response = self.client.post(
            "/v1/events/query",
            headers={"Authorization": "Bearer " + self.key},
            json=continuation,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["next_cursor"])

    def test_auth_and_account_scope_are_enforced(self):
        response = self.client.post("/v1/events/query", json=self.query)
        self.assertEqual(response.status_code, 403)

        mismatched = {**self.query, "account_id": "bob"}
        response = self.client.post(
            "/v1/events/query",
            headers={"Authorization": "Bearer " + self.key},
            json=mismatched,
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
