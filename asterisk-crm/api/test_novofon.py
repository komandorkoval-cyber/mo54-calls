import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from novofon import (
    call_session_id,
    event_idempotency_key,
    event_type,
    normalize_employee_id,
    normalize_phone,
    NovofonClient,
    recording_url,
)


class NovofonHelperTests(unittest.TestCase):
    def test_normalizes_russian_mobile_and_city_numbers(self):
        self.assertEqual(normalize_phone("+7 (383) 235-92-77"), "73832359277")
        self.assertEqual(normalize_phone("8 913 123 45 67"), "79131234567")
        self.assertIsNone(normalize_phone("not a number"))

    def test_call_end_key_is_stable_for_duplicate_delivery(self):
        event = {
            "event": "CALL_END",
            "call_session_id": "session-123",
            "notification_time": "2026-08-05T12:00:00+07:00",
            "external_id": "intent-456",
            "integration_key": "will-be-removed-by-handler",
        }
        sanitized = {key: value for key, value in event.items() if key != "integration_key"}
        self.assertEqual(event_type(sanitized), "CALL_END")
        self.assertEqual(call_session_id(sanitized), "session-123")
        self.assertEqual(event_idempotency_key(sanitized), event_idempotency_key(dict(sanitized)))

    def test_recording_url_accepts_only_https(self):
        payload = {"event": "RECORD_CALL", "call_record_file_info": {"file_link": "https://files.novofon.ru/r/abc"}}
        self.assertEqual(recording_url(payload), "https://files.novofon.ru/r/abc")
        payload["call_record_file_info"]["file_link"] = "http://files.novofon.ru/r/abc"
        self.assertIsNone(recording_url(payload))

    def test_recording_notification_without_explicit_type_is_record_call(self):
        payload = {"call_session_id": "session-42", "file_link": "https://files.novofon.ru/r/abc"}
        self.assertEqual(event_type(payload), "RECORD_CALL")

    def test_employee_id_is_a_positive_json_number(self):
        self.assertEqual(normalize_employee_id("100"), 100)
        self.assertEqual(normalize_employee_id(42), 42)
        self.assertIsNone(normalize_employee_id("0"))
        self.assertIsNone(normalize_employee_id("100.0"))
        self.assertIsNone(normalize_employee_id("employee-100"))

    def test_call_api_sends_employee_id_as_json_number(self):
        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jsonrpc": "2.0", "result": {"data": {"call_session_id": 123}}}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, *, json, headers):
                captured.update({"url": url, "json": json, "headers": headers})
                return FakeResponse()

        async def invoke():
            client = NovofonClient("token", "73832359277", "https://callapi-jsonrpc.novofon.ru/v4.0")
            return await client.start_employee_call(
                request_id="request-123",
                employee_id="100",
                employee_phone="79131234567",
                contact_phone="79001234567",
            )

        with patch("novofon.httpx.AsyncClient", FakeAsyncClient):
            asyncio.run(invoke())
        self.assertEqual(captured["json"]["params"]["employee"]["id"], 100)
        self.assertIsInstance(captured["json"]["params"]["employee"]["id"], int)


if __name__ == "__main__":
    unittest.main()
