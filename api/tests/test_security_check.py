"""The deployment gate checks access denial, not only an HTTP-200 heartbeat."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from pydantic import SecretStr
from inventory_hub import security_check as probe


class SecurityCheckTests(unittest.TestCase):
    def run_probe(self, overrides=None, health='ok'):
        observed = []
        token = 'synthetic-deployment-check-token-123456'
        overrides = overrides or {}

        def read(request, timeout):
            authenticated = request.get_header('Authorization') is not None
            path = request.full_url.replace('https://hub.test', '')
            observed.append((path, authenticated))
            if authenticated:
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + token)
            status = overrides.get((path, authenticated), 200 if path == '/api/health' or authenticated else 401)
            if path.startswith('/data/') and authenticated:
                status = overrides.get((path, authenticated), 404)
            if status != 200:
                raise HTTPError(request.full_url, status, 'fixture', {}, None)
            body = json.dumps({'status': health, 'use_postgres': True, 'database': {'status': 'healthy'}}).encode()
            response = io.BytesIO(body)
            response.status = status
            return response

        output = io.StringIO()
        with patch.object(probe.settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), \
                patch.object(probe, '_open', read), contextlib.redirect_stdout(output):
            result = probe.check('https://hub.test')
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn('Authorization', output.getvalue())
        return result, observed

    def test_read_only_probe_requires_both_authentication_and_private_file_denial(self):
        result, calls = self.run_probe()
        self.assertTrue(result)
        self.assertEqual(len(calls), 6)

    def test_public_exposure_degraded_health_or_failed_authentication_fails_deployment(self):
        for override in ({('/api/stock/summary', False): 200},
                         {('/api/ai-content/status', True): 401},
                         {('/data/shops/biketrek/config.json', True): 200}):
            self.assertFalse(self.run_probe(override)[0])
        self.assertFalse(self.run_probe(health='degraded')[0])

    def test_redirects_are_not_followed_with_credentials(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test'))
