import unittest
from datetime import timezone

from app.event_query import EvidenceWindowSelector, IncidentTimeResolver, IncidentWindow, parse_instant
from app.business_accounts import DiagnosticDataGap


class EventQueryTests(unittest.TestCase):
    def test_explicit_and_relative_times_are_normalized_to_utc(self):
        resolver = IncidentTimeResolver(timezone_offset="+08:00", before_minutes=15, after_minutes=15)
        explicit = resolver.resolve("ERP 在 2026-10-27 10:30 报错", reference_at="2026-10-27T12:00:00+08:00")
        self.assertEqual(explicit["status"], "bounded")
        self.assertEqual(parse_instant(explicit["window"]["start_at"]), parse_instant("2026-10-27T10:15:00+08:00"))
        relative = resolver.resolve("今天上午 10 点 VPN 超时", reference_at="2026-10-27T12:00:00+08:00")
        self.assertEqual(relative["status"], "bounded")
        self.assertEqual(relative["window"]["basis"], "user_explicit_time")

    def test_ambiguous_multiple_times_and_missing_time_do_not_query(self):
        resolver = IncidentTimeResolver()
        for text, reason in (("ERP 一直报错", "date_and_time_required"),
                             ("今天 9:00 到 10:00 又在 11:00 失败", "multiple_incident_times"),
                             ("ERP 明天 10:00 可能会失败", "future_incident_time")):
            result = resolver.resolve(text, reference_at="2026-10-27T12:00:00+08:00")
            self.assertEqual(result["status"], "unspecified" if reason == "date_and_time_required" else "ambiguous")
            self.assertEqual(result["reason"], reason)

    def test_window_selector_follows_only_current_client_session(self):
        account = {"owner_id": "alice", "client_events": [
            {"id": "old", "text": "old", "event_at": "2026-10-27T08:00:00+08:00", "session_ref": "old-session"},
            {"id": "current", "text": "current", "event_at": "2026-10-27T10:30:00+08:00", "session_ref": "current-session"},
        ], "sessions": {
            "old-session": [{"id": "old-log", "text": "old", "event_at": "2026-10-27T08:00:01+08:00"}],
            "current-session": [{"id": "current-log", "text": "current", "event_at": "2026-10-27T10:30:01+08:00"}],
        }}
        window = IncidentWindow("2026-10-27T10:15:00+08:00", "2026-10-27T10:45:00+08:00", "user_explicit_time")
        selected, meta = EvidenceWindowSelector().select(account, window)
        self.assertEqual([row["id"] for row in selected["client_events"]], ["current"])
        self.assertEqual(list(selected["sessions"]), ["current-session"])
        self.assertEqual(meta["excluded_client_records"], 1)

    def test_missing_timestamps_are_a_data_gap(self):
        account = {"owner_id": "alice", "client_events": [{"id": "one", "text": "no timestamp", "session_ref": "s"}], "sessions": {"s": []}}
        window = IncidentWindow("2026-10-27T10:00:00+08:00", "2026-10-27T11:00:00+08:00", "user_explicit_time")
        with self.assertRaisesRegex(DiagnosticDataGap, "时间戳"):
            EvidenceWindowSelector().select(account, window)


if __name__ == "__main__":
    unittest.main()
