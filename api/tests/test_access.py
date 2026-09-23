import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import SecretStr

from inventory_hub import access


class OperatorAccessTests(unittest.TestCase):
    def test_unconfigured_token_fails_closed_for_both_features(self):
        for token in ("", "short"):
            with patch.object(access.settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)):
                for guard, code in ((access.ai_access, "ai"), (access.operator_access, "hub")):
                    with self.subTest(feature=code), self.assertRaises(HTTPException) as error:
                        guard("Bearer " + token)
                    self.assertEqual(error.exception.status_code, 503)
                    self.assertEqual(error.exception.detail["code"], code + "_access_not_configured")

    def test_one_existing_credential_unlocks_both_protected_features(self):
        token = "isolated-operator-test-token-12345"
        with patch.object(access.settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)):
            access.ai_access("Bearer " + token)
            access.operator_access("Bearer " + token)
            with self.assertRaises(HTTPException) as error:
                access.operator_access("Bearer invalid")
            self.assertEqual(error.exception.status_code, 401)
            self.assertEqual(error.exception.detail["code"], "hub_access_required")
            self.assertNotIn(token, str(error.exception.detail))
