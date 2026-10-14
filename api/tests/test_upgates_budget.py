"""Shared quota admission is not permission to retry an uncertain write."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

import requests

from inventory_hub.services import stock_publication_source as stock_transport
from inventory_hub.services import upgates, upgates_budget as budget


BASE = "https://budget-test.invalid/api/v2"
AT = datetime(2026, 10, 14, 20, 30, tzinfo=timezone.utc)
HEADERS = {"X-Rate-Limit-Hour": "50", "X-Rate-Limit-Day": "600",
           "X-Rate-Limit-Hour-Remaining": "2", "X-Rate-Limit-Day-Remaining": "3"}


def response(status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(HEADERS if headers is None else headers)
    result._content = b'{}'
    result.raw = Mock()
    return result


class BudgetRulesTests(unittest.TestCase):
    def test_unknown_is_one_probe_then_pause_and_valid_headers_clear_pause(self):
        state = budget.BudgetState()
        state.reserve(AT)
        self.assertIsNone(state.hour_remaining)
        with self.assertRaises(budget.AdmissionDenied):
            state.reserve(AT)
        state.observe(HEADERS, 200, AT)
        for _ in range(5):
            state.reserve(AT)
        self.assertEqual((state.hour_remaining, state.day_remaining), (0, 0))
        with self.assertRaises(budget.AdmissionDenied) as denied:
            state.reserve(AT)
        self.assertEqual(denied.exception.retry_after, 1801)
        self.assertEqual(state.requests_started, 6)
        state.reserve(AT + timedelta(hours=1))
        self.assertIsNone(state.hour_remaining)

    def test_429_cooldown_is_shared_and_malformed_headers_do_not_invent_quota(self):
        state = budget.BudgetState()
        state.reserve(AT)
        state.observe({**HEADERS, "Retry-After": "120"}, 429, AT)
        with self.assertRaises(budget.AdmissionDenied) as denied:
            state.reserve(AT + timedelta(seconds=1))
        self.assertEqual(denied.exception.retry_after, 119)
        state.observe({"X-Rate-Limit-Total-Remaining": "99999"}, 200, AT)
        self.assertEqual((state.hour_remaining, state.day_remaining), (2, 3))
        self.assertEqual(state.blocked_until, AT + timedelta(seconds=120))

    def test_bad_remaining_values_stay_unknown_and_retry_after_is_bounded(self):
        for value in ("-1", "NaN", "1000000001", "１", "3.5", None):
            state = budget.BudgetState()
            state.reserve(AT)
            state.observe({**HEADERS, "X-Rate-Limit-Hour-Remaining": value}, 200, AT)
            self.assertIsNone(state.hour_remaining)
        self.assertEqual(budget._retry_after("Wed, 14 Oct 2026 20:32:00 GMT", AT), 120)
        self.assertEqual(budget._retry_after("999999999", AT), 604800)
        self.assertEqual(budget._retry_after("private-token", AT), 60)

    def test_origin_group_ignores_api_path_and_never_stores_credentials(self):
        self.assertEqual(budget.target_key(BASE), budget.target_key("https://BUDGET-test.invalid:443/other"))
        self.assertNotEqual(budget.target_key(BASE), budget.target_key("https://other.invalid/api"))
        for url in ("http://budget-test.invalid", "https://user:key@budget-test.invalid", BASE + "?secret=value"):
            with self.assertRaises(budget.AdmissionDenied) as error:
                budget.target_key(url)
            self.assertNotIn(url, str(error.exception))


class GuardedSessionTests(unittest.TestCase):
    def test_every_configured_session_is_guarded_including_direct_source_calls(self):
        with patch.object(upgates, "load_shop_config", return_value={
                "upgates_api_base_url": BASE, "upgates_login": "synthetic-user", "upgates_api_key": "synthetic-key"}):
            client = upgates.UpgatesClient.from_shop("biketrek")
        self.assertIsInstance(client.session, budget.BudgetSession)
        self.assertEqual(client.session.auth, ("synthetic-user", "synthetic-key"))
        with patch.object(budget, "acquire", side_effect=budget.AdmissionDenied(42)), \
                patch.object(requests.Session, "request") as network:
            with self.assertRaises(stock_transport.SourceError) as error:
                stock_transport._request(client, "put", "products", payload={"products": []})
        self.assertEqual(error.exception.code, "stock_publication_rate_limited")
        self.assertFalse(error.exception.uncertain)
        self.assertIs(error.exception.request_sent, False)
        self.assertEqual(error.exception.retry_after, 42)
        network.assert_not_called()
        client.session.close()

    def test_put_failure_is_attempted_once_and_releases_permit_without_retry(self):
        session, permit = budget.BudgetSession(BASE), Mock()
        with patch.object(budget, "acquire", return_value=permit), \
                patch.object(requests.Session, "request", side_effect=requests.Timeout("synthetic")) as network:
            with self.assertRaises(requests.Timeout):
                session.put(BASE + "/products", json={})
        network.assert_called_once()
        permit.close.assert_called_once()
        permit.observe.assert_not_called()

    def test_stream_holds_permit_until_response_or_session_closes_exactly_once(self):
        for close_session in (False, True):
            with self.subTest(close_session=close_session):
                session, permit = budget.BudgetSession(BASE), Mock()
                with patch.object(budget, "acquire", return_value=permit), \
                        patch.object(requests.Session, "request", return_value=response()) as network:
                    result = session.get(BASE + "/products", stream=True)
                permit.close.assert_not_called()
                self.assertFalse(network.call_args.kwargs["allow_redirects"])
                session.close() if close_session else result.close()
                result.close()
                session.close()
                permit.close.assert_called_once()

    def test_remote_429_is_returned_without_retry_and_preserves_retry_header(self):
        session, permit = budget.BudgetSession(BASE), Mock()
        remote = response(429, {"Retry-After": "70"})
        with patch.object(budget, "acquire", return_value=permit), \
                patch.object(requests.Session, "request", return_value=remote) as network:
            result = session.post(BASE + "/orders", json={})
        self.assertIs(result, remote)
        self.assertTrue(result.hub_request_sent)
        permit.observe.assert_called_once_with(remote.headers, 429)
        permit.close.assert_called_once()
        network.assert_called_once()

    def test_database_failure_never_falls_back_or_sends_and_cross_origin_is_rejected(self):
        with patch.object(budget.settings, "USE_POSTGRES", True), \
                patch.object(budget, "_connect", side_effect=RuntimeError("private database secret")), \
                patch.object(budget, "LocalPermit") as local, \
                patch.object(requests.Session, "request") as network:
            session = budget.BudgetSession(BASE)
            result = session.get(BASE + "/status")
            crossed = session.get("https://other.invalid/status")
        self.assertEqual(result.status_code, 429)
        self.assertFalse(result.hub_request_sent)
        self.assertEqual(crossed.status_code, 429)
        self.assertNotIn("secret", result.text)
        local.assert_not_called()
        network.assert_not_called()

    def test_explicit_legacy_mode_shares_cooldown_across_separate_sessions(self):
        with patch.object(budget.settings, "USE_POSTGRES", False), \
                patch.object(budget, "now", return_value=AT), \
                patch.object(budget, "_local_registry", {}), \
                patch.object(requests.Session, "request", return_value=response(429, {"Retry-After": "37"})) as network:
            first = budget.BudgetSession(BASE).get(BASE + "/orders")
            second = budget.BudgetSession(BASE).put(BASE + "/products", json={})
        self.assertTrue(first.hub_request_sent)
        self.assertFalse(second.hub_request_sent)
        self.assertEqual(second.headers["Retry-After"], "37")
        network.assert_called_once()


if __name__ == "__main__":
    unittest.main()
