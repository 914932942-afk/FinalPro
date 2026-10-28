import json
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_diagnostic_review import build, score


class DiagnosticReviewTests(unittest.TestCase):
    def test_packet_hides_expected_labels_and_score_rejects_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = root / "report.json"
            inputs = root / "inputs.jsonl"
            report.write_text(json.dumps({"mode": "live_diagnostic_subflow", "records": [{
                "id": "case-1", "contract_passed": True, "session": {"assessment": "insufficient", "summary": "need evidence",
                    "next_check": "check current session", "citations": [], "observations": [], "stop_reason": "model_finish"}}]}, ensure_ascii=False), encoding="utf-8")
            inputs.write_text(json.dumps({"id": "case-1", "resource": "vpn", "request": "VPN failed", "expected": {"allowed_runbooks": ["hidden"]}}) + "\n", encoding="utf-8")
            out = root / "review"
            build(report, inputs, out)
            packet = json.loads((out / "review-packet.json").read_text(encoding="utf-8"))
            item = packet["items"][0]
            self.assertNotIn("expected", json.dumps(item))
            self.assertIsNone(item["human_review"]["evidence_supported"])
            reviewed = json.loads(json.dumps(packet))
            reviewed["items"][0]["human_review"] = {"reviewer": "r1", "evidence_supported": True,
                "procedure_applicable": True, "handoff_useful": True, "uncertainty_preserved": True, "notes": "checked"}
            reviewed_path = root / "reviewed.json"; reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
            mapping = out / "private-mapping.json"
            result = score(out / "review-packet.json", reviewed_path, mapping, root / "score.json")
            self.assertTrue(result["complete"])
            self.assertEqual(result["passed"], 1)
            reviewed["items"][0]["summary"] = "tampered"
            reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed"):
                score(out / "review-packet.json", reviewed_path, mapping, root / "score2.json")


if __name__ == "__main__":
    unittest.main()
