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
        self.assertEqual(set(result), {"entries", "raw_count", "page", "number_of_pages", "number_of_items", "has_more"})
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

    async def test_incremental_window_includes_recent_changes_to_pre_cutover_orders_only(self):
        created = START - timedelta(days=3650)
        raw = order(creation_time=created.isoformat())
        for deleted in (False, True):
            result, fetch = await self.load(payload(raw), created_from=source.OLDEST_ORDER_CREATION, deleted=deleted)
            params = fetch.call_args.args[1]
            self.assertEqual(params['creation_time_from'], '1900-01-01T00:00:00+00:00')
            self.assertEqual(params['last_update_time_from'], START.isoformat())
            self.assertEqual(params['creation_time_to'], END.isoformat())
            self.assertEqual(result['entries'][0]['created_at'], created.isoformat())
            self.assertEqual(result['entries'][0]['deleted'], deleted)
        # Widening creation coverage never widens the change window: old,
        # unchanged history must not become a full-history scan every poll.
        result, _ = await self.load(payload({**raw, 'last_update_time': (START - timedelta(seconds=1)).isoformat()}),
                                    created_from=source.OLDEST_ORDER_CREATION)
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["raw_count"], 1)

    async def test_utc_normalization_inclusive_creation_window_and_no_invented_update_upper_bound(self):
        raw = order(creation_time="2026-09-23T10:00:00+02:00", last_update_time=(END + timedelta(seconds=1)).isoformat())
        result, _ = await self.load(payload(raw))
        self.assertEqual(result["entries"][0]["created_at"], START.isoformat())
        self.assertEqual(result["entries"][0]["updated_at"], (END + timedelta(seconds=1)).isoformat())
        await self.load(payload(order(creation_time=END.isoformat(), last_update_time=END.isoformat())))

    async def test_fractional_window_uses_seconds_envelope_without_losing_overlap_or_source_precision(self):
        cutoff = START + timedelta(microseconds=600000)
        changed = START + timedelta(minutes=5, microseconds=700000)
        until = END + timedelta(microseconds=250000)
        before_cutoff = order(creation_time=(START + timedelta(microseconds=500000)).isoformat(),
                              last_update_time=(START + timedelta(minutes=5)).isoformat())
        exact_end = order(order_number='AT-END', creation_time=until.isoformat(), last_update_time=until.isoformat())
        upper_overlap = order(order_number='WIRE-END', creation_time=(END + timedelta(seconds=1)).isoformat(),
                              last_update_time=(END + timedelta(seconds=1)).isoformat())
        for deleted in (False, True):
            result, fetch = await self.load(payload(before_cutoff, exact_end, upper_overlap),
                created_from=cutoff, changed_from=changed, created_to=until, deleted=deleted)
            params = fetch.call_args.args[1]
            self.assertEqual(params['creation_time_from'], START.isoformat())
            self.assertEqual(params['last_update_time_from'], (START + timedelta(minutes=5)).isoformat())
            self.assertEqual(params['creation_time_to'], (END + timedelta(seconds=1)).isoformat())
            self.assertEqual((len(result['entries']), result['number_of_items']), (3, 3), 'Whole page counts include read overlap')
            self.assertEqual(result['entries'][1]['created_at'], until.isoformat(), 'Fractional source timestamps are not truncated')
            self.assertLess(datetime.fromisoformat(result['entries'][0]['created_at']), cutoff,
                            'A pre-cutover overlap remains identifiable as historical review')
            self.assertEqual([entry['deleted'] for entry in result['entries']], [deleted] * 3)
        self.assertEqual((cutoff.microsecond, changed.microsecond, until.microsecond), (600000, 700000, 250000))

    async def test_fractional_response_outside_actual_wire_envelope_still_fails_the_whole_page(self):
        changes = dict(created_from=START + timedelta(microseconds=600000),
                       changed_from=START + timedelta(minutes=5, microseconds=700000),
                       created_to=END + timedelta(microseconds=250000))
        for row, reason in (
            (order(creation_time=(START - timedelta(microseconds=1)).isoformat()), 'created_window'),
            (order(creation_time=(END + timedelta(seconds=1, microseconds=1)).isoformat(),
                   last_update_time=(END + timedelta(seconds=2)).isoformat()), 'created_window'),
        ):
            with self.subTest(row=row['order_number']), self.assertRaises(source.CollectionSourceError) as raised:
                await self.load(payload(row), **changes)
            self.assertEqual(raised.exception.code, f'order_collection_invalid_response_{reason}')

    async def test_fractional_timezone_and_year_boundary_round_outwards_in_utc(self):
        zone = timezone(timedelta(hours=2))
        start = datetime(2027, 1, 1, 1, 59, 59, 400000, tzinfo=zone)
        until = datetime(2026, 12, 31, 23, 59, 59, 800000, tzinfo=timezone.utc)
        row = order(creation_time=start.isoformat(), last_update_time=until.isoformat())
        result, fetch = await self.load(payload(row), created_from=start, changed_from=start, created_to=until)
        self.assertEqual(fetch.call_args.args[1]['creation_time_from'], '2026-12-31T23:59:59+00:00')
        self.assertEqual(fetch.call_args.args[1]['last_update_time_from'], '2026-12-31T23:59:59+00:00')
        self.assertEqual(fetch.call_args.args[1]['creation_time_to'], '2027-01-01T00:00:00+00:00')
        self.assertEqual(result['entries'][0]['created_at'], '2026-12-31T23:59:59.400000+00:00')

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
            self.assertEqual(raised.exception.code, "order_collection_invalid_response_metadata")
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

    async def test_rejection_categories_never_expose_source_values_or_accept_invalid_pages(self):
        private = "PRIVATE-CUSTOMER-AND-CONNECTION-DATA"
        raw = order(order_number=private, customer={"email": private}, internal_note=private)
        cases = (
            ("metadata", dict(payload(raw), current_page_items=private), {}),
            ("identity", payload({**raw, "uuid": private}), {}),
            ("identity", payload({**raw, "status_id": private}), {}),
            ("identity", payload({**raw, "origin": None}), {}),
            ("timestamp", payload({**raw, "creation_time": private}), {}),
            ("timestamp", payload({**raw, "last_update_time": private}), {}),
            ("created_window", payload({**raw, "creation_time": (START - timedelta(seconds=1)).isoformat()}), {}),
            ("update_before_creation", payload({**raw, "last_update_time": (START - timedelta(seconds=1)).isoformat()}), {}),
            ("deleted_scope", payload({**raw, "deleted_yn": True}), {}),
            ("duplicate_identity", payload(raw, {**raw, "order_number": "OTHER"}), {}),
            ("duplicate_identity", payload(raw, {**raw, "uuid": str(uuid4())}), {}),
            ("ordering", payload({**raw, "last_update_time": END.isoformat()}, order(order_number="OTHER")), {}),
        )
        for reason, body, arguments in cases:
            with self.subTest(reason=reason), self.assertRaises(source.CollectionSourceError) as raised:
                await self.load(body, **arguments)
            error = raised.exception
            self.assertEqual(error.code, f"order_collection_invalid_response_{reason}")
            self.assertEqual(str(error), error.code)
            self.assertEqual(error.status, 502)
            self.assertLessEqual(len(error.code), 80, "Fits the existing run/settings error columns")
            self.assertNotIn(private, json.dumps(vars(error)))

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
        result, _ = await self.load(payload(order()), changed_from=END)
        self.assertEqual(result["entries"], [])

    async def test_stale_rows_are_filtered_without_losing_raw_page_coverage(self):
        old = order(order_number="OLD", last_update_time=(START - timedelta(minutes=1)).isoformat(),
                    creation_time=(START - timedelta(days=1)).isoformat())
        fresh = order(order_number="NEW")
        result, _ = await self.load(payload(old, fresh, total=2),
                                    created_from=source.OLDEST_ORDER_CREATION)
        self.assertEqual([entry["order_number"] for entry in result["entries"]], ["NEW"])
        self.assertEqual(result["raw_count"], 2)
        self.assertEqual(result["number_of_items"], 2)

    async def test_invalid_request_never_reaches_upstream(self):
        for change in ({"created_from": START.replace(tzinfo=None)}, {"changed_from": START.isoformat()},
                       {"created_from": END, "created_to": START}, {"page": 0}, {"page": True},
                       {"deleted": "true"}, {"created_to": datetime.max.replace(tzinfo=timezone.utc)}):
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

    def test_http_400_has_a_sanitized_diagnostic_without_body_url_or_retry(self):
        client, response = self.client(status=400)
        response.json.return_value = {'messages': ['PRIVATE-CUSTOMER-AND-CONNECTION']}
        response.text = 'PRIVATE-CUSTOMER-AND-CONNECTION'
        response.url = 'https://private.invalid/orders?credential=PRIVATE-CUSTOMER-AND-CONNECTION'
        with patch.object(source.UpgatesClient, 'from_shop', return_value=client), patch.object(client, '_log') as log, \
                self.assertRaises(source.CollectionSourceError) as raised:
            source._fetch_page('biketrek', {'page': 1})
        self.assertEqual((raised.exception.code, raised.exception.status, raised.exception.retry_after),
                         ('order_collection_upgates_http_400', 502, None))
        self.assertEqual(str(raised.exception), 'order_collection_upgates_http_400')
        self.assertNotIn('PRIVATE', repr(vars(raised.exception)))
        response.json.assert_not_called()
        client.session.get.assert_called_once()
        client.session.post.assert_not_called()
        client.session.put.assert_not_called()
        client.session.close.assert_called_once()
        response.close.assert_called_once()
        log.assert_not_called()

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
