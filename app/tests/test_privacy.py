from __future__ import annotations

import unittest

from core.privacy import redact_sensitive_text, sanitize_value


class PrivacyTest(unittest.TestCase):
    def test_text_credentials_are_redacted(self):
        text = "key=sk-abcdefgh12345678 password=hunter2 Bearer abcdefghijklmnop"
        redacted = redact_sensitive_text(text)
        self.assertNotIn("sk-abcdefgh", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_sensitive_mapping_keys_are_never_returned(self):
        warnings: list[str] = []
        value = sanitize_value(
            {
                "name": "Sirius",
                "api_key": "top-secret",
                "nested": {"private_key": "-----BEGIN PRIVATE KEY-----"},
            },
            warnings=warnings,
        )
        self.assertEqual(value["name"], "Sirius")
        self.assertEqual(value["api_key"], "[REDACTED]")
        self.assertEqual(value["nested"]["private_key"], "[REDACTED]")
        self.assertGreaterEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
