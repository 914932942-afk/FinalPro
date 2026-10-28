import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app.public_incidents import PublicIncidentCollector


class PublicIncidentTests(unittest.TestCase):
    def fixture(self):
        fields = ["number", "incident_state", "sys_updated_at", "reopen_count", "reassignment_count", "sys_mod_count",
                  "active", "made_sla", "resolved_at", "closed_at", "priority"]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        rows = [
            ("INC-A", "New", "01/02/2016 10:00", "0"),
            ("INC-A", "Active", "01/02/2016 11:00", "1"),
            ("INC-A", "Resolved", "01/02/2016 12:00", "2"),
            ("INC-B", "New", "01/02/2016 10:00", "0"),
            ("INC-B", "Awaiting User", "01/02/2016 11:00", "1"),
            ("INC-B", "Awaiting User", "01/02/2016 11:00", "1"),
            ("INC-B", "Closed", "01/02/2016 12:00", "2"),
            ("INC-C", "New", "unknown-time", "0"),
        ]
        for number, state, timestamp, modification in rows:
            writer.writerow(dict(number=number, incident_state=state, sys_updated_at=timestamp, reopen_count="0", reassignment_count="2",
                                 sys_mod_count=modification, active="true", made_sla="false", resolved_at="01/02/2016 12:00",
                                 closed_at="01/02/2016 13:00", priority="?"))
        stream.seek(0)
        return stream

    def test_unknowns_duplicates_and_future_fields_preserved_correctly(self):
        with tempfile.TemporaryDirectory() as folder:
            result = PublicIncidentCollector().derive(self.fixture(), folder)
            self.assertEqual(result["events"], 8)
            self.assertEqual(result["incidents"], 3)
            self.assertEqual(result["equal_projected_events_retained"], 1)
            self.assertEqual(result["incidents_with_invalid_time"], 1)
            # B has an ambiguous equal-time cut, C has an invalid timestamp.
            self.assertEqual(result["prefix_cases"], 1)
            inputs = [json.loads(r) for r in (Path(folder) / "workflow-inputs.jsonl").read_text().splitlines()]
            labels = [json.loads(r) for r in (Path(folder) / "workflow-labels.jsonl").read_text().splitlines()]
            self.assertFalse(inputs[0]["synthetic"])
            self.assertIsNone(inputs[0]["source_timezone"])
            self.assertIsNone(inputs[0]["resolution_verified"])
            self.assertIsNone(inputs[0]["events"][0]["priority"])
            self.assertEqual(inputs[0]["events"][0]["event_at"], "2016-02-01T10:00:00")
            self.assertEqual(labels[0]["observed_next_state"], "Active")
            self.assertNotIn("resolved_at", json.dumps(inputs))
            self.assertNotIn("closed_at", json.dumps(inputs))
            self.assertNotIn("made_sla", json.dumps(inputs))
            self.assertIsNone(labels[0]["root_cause"])

    def test_public_schema_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "schema"):
                PublicIncidentCollector().derive(io.StringIO("number,state\n1,Closed\n"), folder)

    def test_public_collection_keeps_source_and_explicit_transport(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("incident_event_log.csv", self.fixture().getvalue())
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "public"
            with patch("app.public_incidents.urlopen", return_value=io.BytesIO(archive.getvalue())) as request:
                manifest = PublicIncidentCollector().collect(target)
            self.assertTrue(request.call_args.args[0].full_url.startswith("https://"))
            self.assertEqual(manifest["transport"], "https")
            self.assertEqual((target / "source.zip").read_bytes(), archive.getvalue())
            self.assertEqual(manifest["events"], 8)
            self.assertTrue((target / "ATTRIBUTION.md").is_file())


if __name__ == "__main__":
    unittest.main()
