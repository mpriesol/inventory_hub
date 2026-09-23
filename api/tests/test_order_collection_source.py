"""Order change discovery contracts; synthetic HTTP responses only."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import hashlib
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from inventory_hub.services import order_collection_source as source
from inventory_hub.services import upgates
from inventory_hub.services.upgates import UpgatesClient


START = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def order(**changes):
    return {"order_number": "ORDER-1", "uuid": str(uuid4()), "creation_time": START.isoformat(),
            "last_update_time": (START + timedelta(minutes=5)).isoformat(), "status_id": 1,
            "origin": "frontend", **changes}


def payload(*rows, page=1, pages=1, total=None):
    return {"orders": list(rows), "current_page": page, "current_page_items": len(rows),
            "number_of_pages": pages, "number_of_items": len(rows) if total is None else total}


class CollectionPageTests(IsolatedAsyncioTestCase):
    async def load(self, body, **changes):
        arguments = {"created_from": START, "changed_from": START, "created_to": END, "page": 1, **changes}
        with patch.object(source, "_fetch_page", return_value=body) as fetch:
            result = await source.load_changed_page("biketrek", **arguments)
        return result, fetch

    async def test_active_page_has_exact_private_projection_and_only_documented_filters(self):
        private = "PRIVATE-CUSTOMER-PRICE"
        raw = order(customer={"email": private}, products=[{"title": private, "price": private}], internal_note=private)
        result, fetch = await self.load(payload(raw))
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual(set(result), {"entries", "page", "number_of_pages", "number_of_items", "has_more"})
        self.assertEqual(set(result["entries"][0]), {"order_number", "uuid", "created_at", "updated_at", "deleted", "status_id", "origin"})
        self.assertFalse(result["entries"][0]["deleted"])
        self.assertFalse(result["has_more"])
        fetch.assert_called_once_with("biketrek", {"creation_time_from": START.isoformat(),
            "creation_time_to": END.isoformat(), "last_update_time_from": START.isoformat(),
            "page": 1, "order_by": "last_update_time", "order_dir": "asc"})

    async def test_deleted_scope_is_explicit_and_needs_no_undocumented_order_flag(self):
        result, fetch = await self.load(payload(order()), deleted=True)
        self.assertTrue(result["entries"][0]["deleted"])
        self.assertEqual(fetch.call_args.args[1]["deleted_yn"], "true")
        with self.assertRaises(source.CollectionSourceError):
            await self.load(payload(order(deleted_yn=False)), deleted=True)

    async def test_utc_normalization_inclusive_creation_window_and_no_invented_update_upper_bound(self):
        raw = order(creation_time="2026-09-23T10:00:00+02:00", last_update_time=(END + timedelta(seconds=1)).isoformat())
        result, _ = await self.load(payload(raw))
        self.assertEqual(result["entries"][0]["created_at"], START.isoformat())
        self.assertEqual(result["entries"][0]["updated_at"], (END + timedelta(seconds=1)).isoformat())
        await self.load(payload(order(creation_time=END.isoformat(), last_update_time=END.isoformat())))

    async def test_empty_results_accept_zero_or_one_reported_page_only(self):
        for pages in (0, 1):
            result, _ = await self.load(payload(pages=pages))
            self.assertEqual(result["entries"], [])
            self.assertFalse(result["has_more"])
        for body, kwargs in ((payload(pages=2), {}), (payload(page=2, pages=2), {"page": 2}),
                             (payload(total=1), {})):
            with self.assertRaises(source.CollectionSourceError):
                await self.load(body, **kwargs)

    async def test_metadata_rejects_inconsistent_counts_missing_fields_and_boolean_numbers(self):
        valid = payload(order())
        bad = [dict(valid, current_page=2), dict(valid, current_page_items=0), dict(valid, number_of_items=2),
               dict(valid, number_of_pages=0), dict(valid, number_of_pages=3, number_of_items=2),
               dict(valid, number_of_pages=2, number_of_items=102), dict(valid, number_of_items=True),
               {key: value for key, value in valid.items() if key != "current_page_items"}]
        for body in bad:
            with self.subTest(keys=body.keys()), self.assertRaises(source.CollectionSourceError) as raised:
                await self.load(body)
            self.assertEqual(raised.exception.code, "order_collection_invalid_response")
        result, _ = await self.load(payload(order(), pages=2, total=101))
        self.assertTrue(result["has_more"])

    async def test_page_size_duplicates_and_nonascending_updates_are_rejected(self):
        first, second = order(), order(order_number="ORDER-2")
        second["uuid"] = first["uuid"]
        bad = [payload(*[order(order_number=f"O-{i}") for i in range(101)]),
               payload(first, second), payload(order(), order()),
               payload(order(last_update_time=END.isoformat()), order(order_number="ORDER-2"))]
        for body in bad:
            with self.assertRaises(source.CollectionSourceError):
                await self.load(body)
        # Ties are legitimate: no undocumented secondary sort is imposed.
        await self.load(payload(order(), order(order_number="ORDER-2")))

    async def test_malformed_identifiers_dates_flags_and_out_of_window_rows_fail_whole_page(self):
        bad = [{"order_number": "A;B"}, {"order_number": " ORDER-1"}, {"order_number": "bad\ud800number"},
               {"uuid": "not-uuid"}, {"status_id": True}, {"origin": None}, {"deleted_yn": 0},
               {"deleted_yn": True}, {"creation_time": "2026-09-23T08:00:00"},
               {"creation_time": (START - timedelta(seconds=1)).isoformat()},
               {"creation_time": (END + timedelta(seconds=1)).isoformat()},
               {"last_update_time": (START - timedelta(seconds=1)).isoformat()}]
        for change in bad:
            with self.subTest(fields=list(change)), self.assertRaises(source.CollectionSourceError):
                await self.load(payload(order(**change)))
        with self.assertRaises(source.CollectionSourceError):
            await self.load(payload(order()), changed_from=END)

    async def test_invalid_request_never_reaches_upstream(self):
        for change in ({"created_from": START.replace(tzinfo=None)}, {"changed_from": START.isoformat()},
                       {"created_from": END, "created_to": START}, {"page": 0}, {"page": True},
                       {"deleted": "true"}):
            with patch.object(source, "_fetch_page") as fetch, self.assertRaises(source.CollectionSourceError) as raised:
                await source.load_changed_page("biketrek", **{"created_from": START, "changed_from": START,
                                                "created_to": END, "page": 1, **change})
            self.assertEqual((raised.exception.code, raised.exception.status), ("order_collection_invalid_request", 422))
            fetch.assert_not_called()

    async def test_expected_target_is_passed_to_the_actual_client_read(self):
        fingerprint = source.connection_fingerprint("https://example.invalid/api/v2", "test")
        _, fetch = await self.load(payload(order()), expected_target_fingerprint=fingerprint)
        self.assertEqual(fetch.call_args.kwargs, {"expected_target_fingerprint": fingerprint})


class CollectionTransportTests(TestCase):
    def client(self, body=None, status=200, retry_after=None):
        client = UpgatesClient("https://example.invalid/api/v2", "test", "test")
        client.session = Mock()
        client.session.auth = ("test", "test")
        response = Mock(status_code=status, content=b"{}", headers={"Retry-After": retry_after})
        response.json.return_value = body if body is not None else payload(order())
        client.session.get.return_value = response
        return client, response

    def test_fingerprint_matches_shared_compact_list_and_rejects_unsafe_connections(self):
        expected = hashlib.sha256(json.dumps(["https", "example.invalid", "/api/v2", "test"], separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(source.connection_fingerprint("https://EXAMPLE.INVALID/api/v2/", "test"), expected)
        for base, login in (("http://example.invalid/api/v2", "test"), ("https://test:secret@example.invalid/api/v2", "test"),
                            ("https://example.invalid/api/v2?x=1", "test"), ("https://example.invalid/#fragment", "test"),
                            ("https://example.invalid", ""), (None, "test")):
            with self.subTest(base=base):
                self.assertIsNone(source.connection_fingerprint(base, login))

    def test_actual_client_target_mismatch_blocks_before_any_get_and_closes_client(self):
        expected = source.connection_fingerprint("https://example.invalid/api/v2", "test")
        for base, login in (("https://other.invalid/api/v2", "test"), ("https://example.invalid/different", "test"),
                            ("https://example.invalid/api/v2", "other-login"), ("http://example.invalid/api/v2", "test")):
            client, response = self.client()
            client.base_url, client.session.auth = base, (login, "secret-not-hashed")
            with self.subTest(base=base, login=login), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.CollectionSourceError) as raised:
                source._fetch_page("biketrek", {}, expected_target_fingerprint=expected)
            self.assertEqual((raised.exception.code, raised.exception.status), ("order_collection_target_changed", 409))
            client.session.get.assert_not_called()
            client.session.close.assert_called_once()
            response.json.assert_not_called()
        client, _ = self.client()
        client.session.auth = ("test", "rotated-key")
        with patch.object(source.UpgatesClient, "from_shop", return_value=client):
            source._fetch_page("biketrek", {}, expected_target_fingerprint=expected)
        client.session.get.assert_called_once()

    def test_one_safe_get_no_status_fetch_retry_log_or_shop_mutation(self):
        client, response = self.client()
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), patch.object(client, "_log") as log:
            source._fetch_page("biketrek", {"page": 1})
        client.session.get.assert_called_once()
        self.assertTrue(client.session.get.call_args.args[0].endswith("/orders"))
        self.assertFalse(client.session.get.call_args.kwargs["allow_redirects"])
        self.assertLessEqual(client.session.get.call_args.kwargs["timeout"][1], 25)
        client.session.post.assert_not_called()
        client.session.put.assert_not_called()
        log.assert_not_called()
        client.session.close.assert_called_once()
        response.close.assert_called_once()

    def test_rate_limit_http_date_is_sanitized_to_seconds_without_body_or_header_leak(self):
        header = format_datetime(START + timedelta(seconds=120), usegmt=True)
        client, response = self.client(status=429, retry_after=header)
        response.json.return_value = {"private": "DO-NOT-LEAK"}
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), \
             patch.object(upgates, "datetime", wraps=datetime) as clock, self.assertRaises(source.CollectionSourceError) as raised:
            clock.now.return_value = START
            source._fetch_page("biketrek", {"page": 1})
        self.assertEqual((raised.exception.code, raised.exception.status, raised.exception.retry_after),
                         ("order_collection_rate_limited", 429, 120))
        self.assertNotIn(header, str(raised.exception))
        self.assertNotIn("DO-NOT-LEAK", str(raised.exception))
        response.json.assert_not_called()
        client.session.get.assert_called_once()

    def test_unsafe_or_missing_retry_headers_are_not_exposed(self):
        for value, expected in (("60", 60), ("private-secret", None), (None, None), ("999999999", 604800)):
            client, _ = self.client(status=429, retry_after=value)
            with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(source.CollectionSourceError) as raised:
                source._fetch_page("biketrek", {})
            self.assertEqual(raised.exception.retry_after, expected)

    def test_auth_bad_json_messages_size_and_connection_failures_are_safe_and_close_session(self):
        for mode in ("auth", "json", "messages", "size", "network"):
            client, response = self.client(status=403 if mode == "auth" else 200)
            if mode == "json":
                response.json.side_effect = ValueError("PRIVATE")
            elif mode == "messages":
                response.json.return_value = {"messages": ["PRIVATE"]}
            elif mode == "size":
                response.content = b"x" * 8_000_001
            elif mode == "network":
                client.session.get.side_effect = requests.Timeout("PRIVATE")
            with self.subTest(mode=mode), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.CollectionSourceError) as raised:
                source._fetch_page("biketrek", {})
            self.assertNotIn("PRIVATE", str(raised.exception))
            client.session.get.assert_called_once()
            client.session.close.assert_called_once()
