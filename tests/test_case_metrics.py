"""Aggregate lifecycle evidence should expose false closures without exporting records."""
from datetime import datetime, timedelta, timezone
import unittest

from app.case_management import CaseLifecycle
from app.case_metrics import summarize_cases


class CaseMetricsTests(unittest.TestCase):
    def test_mixed_cases_detect_unverified_resolution_and_missing_owner(self):
        now = datetime.now(timezone.utc)
        created = (now - timedelta(hours=3)).isoformat()
        active = {"ticket": {
            "status": "awaiting_support", "recommended_action": "open_incident", "created_at": created,
            "assignment": {"team": "应用支持", "group_id": "applications", "assignee": None,
                           "due_at": (now - timedelta(hours=1)).isoformat(), "paused_at": None},
            "diagnostic_report": {"assessment": "not_completed", "stop_reason": "model_error:ModelUnavailableError"},
            "feedback": [], "work_log": [], "case_events": []}, "resolution": None}
        unverified = {"ticket": {
            "status": "resolved", "recommended_action": "open_incident", "created_at": created,
            "assignment": None, "diagnostic_report": None, "feedback": [], "work_log": [], "case_events": []},
            "resolution": {"type": "agent_task_completed", "external_issue_resolved": True}}
        confirmed = {"ticket": {
            "status": "resolved", "recommended_action": "open_incident", "created_at": created,
            "assignment": {"assigned_at": (now - timedelta(hours=2)).isoformat()},
            "diagnostic_report": None, "feedback": [], "work_log": [{"kind": "check"}], "case_events": []},
            "resolution": {"type": "user_confirmed", "external_issue_resolved": True, "at": now.isoformat()}}
        legacy_closed = {"ticket": {
            "status": "closed", "recommended_action": "open_incident", "created_at": created,
            "assignment": None, "diagnostic_report": None, "feedback": [], "work_log": [], "case_events": []},
            "resolution": {"type": "agent_task_completed", "external_issue_resolved": True}}
        result = summarize_cases([active, unverified, confirmed, legacy_closed], now=now)
        metrics = result["metrics"]
        self.assertEqual(metrics["model_interruptions"], 1)
        self.assertEqual(metrics["attention_support"], 1)
        self.assertEqual(metrics["overdue_support"], 1)
        self.assertEqual(metrics["unassigned_support"], 1)
        self.assertEqual(metrics["resolved_without_user_verification"], 2)
        self.assertEqual(metrics["user_verified_resolutions"], 1)
        self.assertEqual(metrics["manual_work_records"], 1)
        self.assertEqual(result["median_hours_to_user_confirmation"], 3)
        self.assertNotIn("T-1", str(result))

    def test_partial_assignment_is_reported_without_crashing(self):
        state = {"ticket": {
            "status": "in_progress", "recommended_action": "open_incident",
            "created_at": datetime.now(timezone.utc).isoformat(), "assignment": {"assignee": "support"},
            "diagnostic_report": None, "feedback": [], "work_log": [], "case_events": []}, "resolution": None}
        self.assertFalse(CaseLifecycle.overdue(state["ticket"]["assignment"]))
        result = summarize_cases([state])
        self.assertEqual(result["metrics"]["missing_support_group"], 1)
        self.assertEqual(result["metrics"]["missing_support_deadline"], 1)
