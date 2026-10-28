import json
import unittest

from app.redaction import redact_payload, redact_text
from app.investigation import Citation, InvestigationStep, validate_citations


class RedactionTests(unittest.TestCase):
    def test_text_redacts_credentials_and_personal_identifiers(self):
        value = (
            "Authorization: Bearer abcdefghijklmnop1234 password=s3cret "
            "email alice@example.com phone 13800138000"
        )
        safe = redact_text(value)
        self.assertNotIn("abcdefghijklmnop1234", safe)
        self.assertNotIn("s3cret", safe)
        self.assertNotIn("alice@example.com", safe)
        self.assertNotIn("13800138000", safe)
        self.assertIn("[REDACTED]", safe)

    def test_nested_model_payload_redacts_sensitive_keys_and_keeps_error_context(self):
        payload = {
            "request": "VPN 返回 691",
            "observations": [{"text": "token=secret-value; error=691"}],
            "headers": {"authorization": "Bearer abcdefghijklmnop1234"},
        }
        safe = redact_payload(payload)
        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertIn("VPN 返回 691", encoded)
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn("abcdefghijklmnop1234", encoded)
        self.assertEqual(safe["headers"]["authorization"], "[REDACTED]")

    def test_citation_accepts_visible_redacted_quote_but_not_original_secret(self):
        observation = [{"records": [{"id": "log-1", "kind": "log",
                                      "text": "token=secret-value; error=691"}]}]
        safe_step = InvestigationStep(
            kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
            summary="证据不足", next_check="人工核查", reason="缺少当前会话",
            citations=[Citation(record_id="log-1", quote="token=[REDACTED]; error=691")],
        )
        validate_citations(safe_step, observation)
        unsafe_step = safe_step.model_copy(update={
            "citations": [Citation(record_id="log-1", quote="token=secret-value; error=691")]
        })
        with self.assertRaises(ValueError):
            validate_citations(unsafe_step, observation)


if __name__ == "__main__":
    unittest.main()
