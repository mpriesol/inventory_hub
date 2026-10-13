"""Credential privacy, bounded tool use and read-only Luna probe contracts."""
import copy
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from inventory_hub.ai_content_types import JobAction
from inventory_hub.routers.sloger_probe import router
from inventory_hub.services import ai_content, catalog_import, sloger_b2b, sloger_probe as service
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import ProbeRequest, SlogerCredentials, SlogerObservation


def observation():
    return {"sku": "PT-TEST-1", "product_name": "Test pump", "eans": ["0012345678901"],
            "manufacturer_code": "M-TEST-1", "retail_price": "22.5", "retail_price_basis": "gross",
            "purchase_price": "12.5", "currency": "EUR", "price_basis": "net",
            "availability": "6+ skladom", "available": True, "quantity": "6", "quantity_kind": "minimum",
            "quantity_relation": "at_least", "availability_tiers": [
                {"label": "Skladom 6+", "quantity": "6", "quantity_kind": "minimum", "quantity_relation": "at_least", "lead_days": None},
                {"label": "Do 5 dní >99", "quantity": "99", "quantity_kind": "minimum", "quantity_relation": "greater_than", "lead_days": 5}],
            "source_url": "https://www.sloger.sk/test-pump/", "observed_at": "2026-10-12T12:00:00Z"}


def tool_response(sku="PT-TEST-1"):
    return {"id": "resp_test_one", "status": "completed", "usage": {"input_tokens": 200, "output_tokens": 50},
            "output": [{"type": "reasoning", "id": "reasoning_test", "summary": [], "encrypted_content": "test-encrypted-state"},
                       {"type": "function_call", "name": "lookup_sloger_product", "call_id": "call_test",
                        "arguments": json.dumps({"sku": sku})}]}


def final_response(facts=None):
    return {"id": "resp_test_two", "status": "completed", "usage": {"input_tokens": 300, "output_tokens": 100},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(facts or observation())}]}]}


class ProbeContractTests(unittest.TestCase):
    def test_luna_requests_have_one_strict_tool_and_preserve_reasoning_continuation(self):
        first = service.request_body("PT-TEST-1")
        self.assertEqual(first["model"], "gpt-6-luna")
        self.assertEqual(first["reasoning"], {"effort": "high"})
        self.assertFalse(first["store"])
        self.assertFalse(first["parallel_tool_calls"])
        self.assertEqual(first["tools"][0]["parameters"]["properties"]["sku"]["enum"], ["PT-TEST-1"])
        self.assertEqual(first["tools"][0]["parameters"]["required"], ["sku"])
        self.assertFalse(first["tools"][0]["parameters"]["additionalProperties"])
        response = tool_response()
        second = service.request_body("PT-TEST-1", previous=response, facts=observation(), call_id="call_test")
        self.assertEqual(second["input"][1:3], response["output"])
        self.assertEqual(second["input"][-1]["type"], "function_call_output")
        self.assertEqual(second["tool_choice"], "none")
        schema = second["text"]["format"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertGreater(service.estimate(), 0)

    def test_extra_tools_wrong_sku_or_arguments_are_refused(self):
        for change in (lambda r: r["output"].append(copy.deepcopy(r["output"][-1])),
                       lambda r: r["output"][-1].update(name="place_order"),
                       lambda r: r["output"][-1].update(arguments='{"sku":"OTHER"}'),
                       lambda r: r["output"][-1].update(arguments='{"sku":"PT-TEST-1","url":"https://example.test"}'),
                       lambda r: r.update(status="incomplete")):
            response = tool_response()
            change(response)
            with self.assertRaises(CatalogError) as caught:
                service.function_call(response, "PT-TEST-1")
            self.assertEqual(caught.exception.code, "sloger_probe_invalid_call")

    def test_result_requires_exact_supplier_evidence_and_never_fills_missing_values(self):
        facts = SlogerObservation.model_validate(observation()).model_dump(mode="json")
        self.assertEqual(service.parse_result(final_response(), facts), facts)
        for change in ({"purchase_price": "13"}, {"retail_price_basis": "net"}, {"quantity_kind": "exact"},
                       {"quantity_relation": "greater_than"}, {"eans": ["0099999999999"]},
                       {"availability_tiers": [{**facts["availability_tiers"][1], "quantity_relation": "at_least"}]}):
            with self.subTest(change=change), self.assertRaises(CatalogError) as caught:
                service.parse_result(final_response({**facts, **change}), facts)
            self.assertEqual(caught.exception.code, "sloger_probe_fact_mismatch")
        unknown = {**facts, "purchase_price": None, "currency": None, "price_basis": "unknown",
                   "available": None, "quantity": None, "quantity_kind": "unknown"}
        self.assertEqual(service.parse_result(final_response(unknown), unknown), unknown)

    def test_credentials_or_extra_fields_cannot_become_observation_fields(self):
        for change in ({"password": "test-secret"}, {"source_url": "https://www.sloger.sk/p/?token=secret"},
                       {"source_url": "https://user:secret@www.sloger.sk/p/"}, {"eans": ["0000000000000"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                SlogerObservation.model_validate({**observation(), **change})
        with self.assertRaises(CatalogError) as caught:
            service.usage_cost({"output": []})
        self.assertEqual(caught.exception.code, "ai_outcome_unknown")
        with self.assertRaises(ValueError):
            ProbeRequest(request_id=uuid4(), username="operator@example.test", password="secret", sku="secret")

    def test_unreadable_usage_is_uncertain_instead_of_a_zero_charge(self):
        response = tool_response()
        response["usage"]["input_tokens_details"] = {"cached_tokens": "unreadable"}
        with self.assertRaises(CatalogError) as caught:
            service.usage_cost(response)
        self.assertEqual(caught.exception.code, "ai_outcome_unknown")


class ProbeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.request = ProbeRequest(request_id=uuid4(), username="operator@example.test",
                                    password="synthetic-password-never-persist", sku="PT-TEST-1")
        self.calls = AsyncMock(side_effect=[tool_response(), final_response()])
        self.adapter = AsyncMock(return_value=observation())
        self.audit = AsyncMock(side_effect=lambda id, **kwargs: {
            "id": id, "status": kwargs.get("status", "lookup_running"), "model": service.MODEL,
            "result": kwargs.get("result"), "error": kwargs.get("error"),
            "actual_usd": str(kwargs["cost"]), "usage": {"calls": kwargs["usages"]}})
        self.credentials = SlogerCredentials(username=self.request.username, password=self.request.password)
        self.patches = [patch.object(service, "reserve", AsyncMock(return_value=({"id": self.request.request_id.hex}, self.credentials))),
                        patch.object(service, "record", self.audit), patch.object(service, "call_provider", self.calls),
                        patch.object(service, "lookup_product", self.adapter),
                        patch.object(catalog_import, "create_preview", AsyncMock(side_effect=AssertionError("No publication"))),
                        patch.object(catalog_import.UpgatesClient, "from_shop", side_effect=AssertionError("No Upgates"))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_two_paid_calls_and_one_exact_lookup_without_credentials_in_model_or_audit(self):
        result = await service.probe(self.request)
        self.assertEqual(result["status"], "lookup_completed")
        self.assertEqual(self.calls.await_count, 2)
        self.adapter.assert_awaited_once_with(self.request.username, self.request.password.get_secret_value(), self.request.sku)
        self.assertEqual(result["result"], observation())
        for call in [*self.calls.call_args_list, *self.audit.call_args_list]:
            serialized = str(call)
            self.assertNotIn(self.request.username, serialized)
            self.assertNotIn(self.request.password.get_secret_value(), serialized)
        self.assertEqual(len(result["usage"]["calls"]), 2)
        self.assertGreater(Decimal(result["actual_usd"]), 0)

    async def test_saved_mode_uses_resolved_credentials_without_sending_them_to_luna_or_audit(self):
        request = ProbeRequest(request_id=self.request.request_id, sku=self.request.sku)
        result = await service.probe(request)
        self.assertEqual(result["status"], "lookup_completed")
        self.adapter.assert_awaited_once_with(self.credentials.username, self.credentials.password.get_secret_value(), request.sku)
        for call in [*self.calls.call_args_list, *self.audit.call_args_list]:
            self.assertNotIn(self.credentials.username, str(call))
            self.assertNotIn(self.credentials.password.get_secret_value(), str(call))

    async def test_replayed_id_returns_prior_outcome_without_any_network_or_audit_write(self):
        prior = {"id": self.request.request_id.hex, "status": "lookup_uncertain"}
        with patch.object(service, "reserve", AsyncMock(return_value=(prior, None))):
            self.assertEqual(await service.probe(self.request), prior)
        self.calls.assert_not_awaited()
        self.adapter.assert_not_awaited()
        self.audit.assert_not_awaited()

    async def test_second_provider_timeout_records_known_first_cost_and_uncertain_status(self):
        self.calls.side_effect = [tool_response(), CatalogError("ai_outcome_unknown", "sanitized", 502)]
        result = await service.probe(self.request)
        self.assertEqual(result["status"], "lookup_uncertain")
        self.assertEqual(result["error"], "ai_outcome_unknown")
        self.assertEqual(len(result["usage"]["calls"]), 1)
        self.assertEqual(self.calls.await_count, 2)

    async def test_wrong_tool_does_not_access_supplier_and_secret_errors_are_redacted(self):
        self.calls.side_effect = [tool_response("WRONG")]
        result = await service.probe(self.request)
        self.assertEqual(result["status"], "lookup_failed")
        self.assertEqual(result["error"], "sloger_probe_invalid_call")
        self.adapter.assert_not_awaited()
        self.calls.side_effect = [tool_response()]
        self.adapter.side_effect = RuntimeError(self.request.password.get_secret_value())
        result = await service.probe(self.request)
        self.assertNotIn(self.request.password.get_secret_value(), str(result))
        self.assertEqual(result["status"], "lookup_failed")

    async def test_secret_in_evidence_never_reaches_second_model_call(self):
        self.adapter.return_value = {**observation(), "product_name": self.request.password.get_secret_value()}
        result = await service.probe(self.request)
        self.assertEqual(result["error"], "sloger_probe_invalid_evidence")
        self.assertEqual(self.calls.await_count, 1)
        self.assertNotIn(self.request.password.get_secret_value(), str(self.audit.call_args_list))

    async def test_saved_special_character_secrets_in_nested_evidence_never_reach_model_or_audit(self):
        request = ProbeRequest(request_id=self.request.request_id, sku=self.request.sku)
        for secret in ('quoted"password', 'backslash\\password', 'newline\npassword'):
            with self.subTest(secret="redacted"):
                credentials = SlogerCredentials(username="saved-login@example.test", password=secret)
                facts = observation()
                facts["availability_tiers"][0]["label"] = "Supplier text: " + secret
                self.adapter.return_value = facts
                self.calls.reset_mock(side_effect=True)
                self.calls.side_effect = [tool_response(), final_response()]
                self.audit.reset_mock()
                with patch.object(service, "reserve", AsyncMock(return_value=({"id": request.request_id.hex}, credentials))):
                    result = await service.probe(request)
                self.assertEqual(result["error"], "sloger_probe_invalid_evidence")
                self.assertEqual(self.calls.await_count, 1)
                # Both literal and JSON-escaped forms stay out of stored/model data.
                for output in (str(self.audit.call_args_list), json.dumps(self.calls.call_args.args[0]), json.dumps(result)):
                    self.assertNotIn(secret, output)
                    self.assertNotIn(json.dumps(secret)[1:-1], output)

    async def test_supplier_failure_records_safe_diagnostic_without_another_paid_call(self):
        error = sloger_b2b._error(reason="http_status", http_status=403)
        error.stage = "login_page"
        self.adapter.side_effect = error
        result = await service.probe(self.request)
        self.assertEqual(result["error"], "sloger_b2b_unavailable")
        diagnostic = self.audit.call_args.kwargs["diagnostic"]
        self.assertEqual(diagnostic, {"stage": "login_page", "reason": "http_status", "http_status": 403})
        self.assertEqual(self.calls.await_count, 1)
        job = SimpleNamespace(id="a" * 32, status="lookup_failed", output=None, usage={}, actual_usd=None,
                              error="sloger_b2b_unavailable", context={"supplier_diagnostic": diagnostic})
        self.assertEqual(service.public(job)["diagnostic"], diagnostic)
        self.assertNotIn(self.request.password.get_secret_value(), str(self.audit.call_args_list))

    async def test_lookup_kind_cannot_start_review_publish_or_be_cancelled_into_another_state(self):
        job = SimpleNamespace(kind="supplier_lookup", revision=1, status="lookup_running")
        for operation in (lambda: ai_content.start(None, job), lambda: ai_content.prepare_import(None, job),
                          lambda: ai_content.action(None, job, JobAction(expected_revision=1, action="cancel"))):
            with self.assertRaises(CatalogError) as caught:
                await operation()
            self.assertEqual(caught.exception.code, "ai_job_kind_unsupported")
        self.assertEqual(job.status, "lookup_running")


class ProbeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_connection_check_is_protected_uncached_and_does_not_invoke_ai(self):
        app = FastAPI()
        app.include_router(router)
        connection = {"reachable": True, "login_form_found": True, "diagnostic": None}
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("test-token-only-12345678901234567890")), \
                patch.object(sloger_b2b, "connection", AsyncMock(return_value=connection)) as check, \
                patch.object(service, "probe", AsyncMock()) as probe:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
                denied = await client.get("/suppliers/sloger/lookup/connection")
                self.assertEqual(denied.status_code, 401)
                check.assert_not_awaited()
                accepted = await client.get("/suppliers/sloger/lookup/connection",
                    headers={"Authorization": "Bearer test-token-only-12345678901234567890"})
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(accepted.headers["Cache-Control"], "no-store")
                self.assertEqual(accepted.json(), connection)
                check.assert_awaited_once_with()
                probe.assert_not_awaited()

    async def test_authentication_and_validation_errors_do_not_echo_request_secrets(self):
        app = FastAPI()
        app.include_router(router)
        payload = {"request_id": str(uuid4()), "username": "operator@example.test",
                   "password": "synthetic-password-never-persist", "sku": "PT-TEST-1"}
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr("test-token-only-12345678901234567890")), \
                patch.object(service, "probe", AsyncMock(return_value={"status": "lookup_completed"})) as probe:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
                denied = await client.post("/suppliers/sloger/lookup/preview", json=payload)
                self.assertEqual(denied.status_code, 401)
                headers = {"Authorization": "Bearer test-token-only-12345678901234567890"}
                bad = await client.post("/suppliers/sloger/lookup/preview", headers=headers, json={**payload, "sku": "x" * 101})
                self.assertEqual(bad.status_code, 422)
                for response in (denied, bad):
                    self.assertNotIn(payload["password"], response.text)
                    self.assertNotIn(payload["username"], response.text)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                probe.assert_not_awaited()
                accepted = await client.post("/suppliers/sloger/lookup/preview", headers=headers, json=payload)
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(accepted.headers["Cache-Control"], "no-store")
                self.assertIsInstance(probe.call_args.args[0].password, SecretStr)
