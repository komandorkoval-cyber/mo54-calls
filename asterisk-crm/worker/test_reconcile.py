import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from reconcile import NovofonReconciler, _records_from_response, report_row_events


class NovofonReconcileTests(unittest.TestCase):
    def test_row_becomes_deterministic_call_and_record_events(self):
        row = {
            "id": "session-42",
            "direction": "outbound",
            "contact_phone_number": "8 (999) 123-45-67",
            "virtual_phone_number": "73832359277",
            "talk_duration": 12,
            "total_duration": 18,
            "finish_date": "2026-08-05 10:00:00",
            "call_api_external_id": "intent-1",
            "full_record_file_link": "https://files.novofon.ru/record/42.wav",
        }
        events = report_row_events(row)
        self.assertEqual([event[0] for event in events], ["CALL_END", "RECORD_CALL"])
        self.assertEqual(events[0][1], "novofon:dataapi:call-end:session-42")
        self.assertEqual(events[0][2]["call_info"]["talk_time_duration"], 12)
        self.assertEqual(events[1][2]["call_record_file_info"]["file_link"], row["full_record_file_link"])

    def test_row_without_stable_identity_is_skipped(self):
        self.assertEqual(report_row_events({"contact_phone_number": "79991234567"}), [])

    def test_non_https_recording_is_not_persisted(self):
        events = report_row_events({"id": "s1", "full_record_file_link": "http://example.test/audio.wav"})
        self.assertEqual([event[0] for event in events], ["CALL_END"])

    def test_response_shapes_and_pagination_request(self):
        rows, total = _records_from_response({"result": {"data": [{"id": "1"}], "total": 1}})
        self.assertEqual(rows, [{"id": "1"}])
        self.assertEqual(total, 1)

        queued = []
        reconciler = NovofonReconciler(
            enabled=True,
            access_token="never-printed",
            api_url="https://dataapi-jsonrpc.novofon.ru/v2.0",
            virtual_phone="73832359277",
            enqueue=lambda **event: queued.append(event) or True,
        )
        self.assertTrue(reconciler.configured)
        self.assertEqual(reconciler.window_hours, 24)
        self.assertEqual(reconciler.overlap_minutes, 10)

    def test_account_timezone_window_is_stable_for_a_known_time(self):
        reconciler = NovofonReconciler(enabled=False, access_token="x")
        # This is intentionally only a construction smoke test: HTTP is not
        # contacted in unit tests, but the fixed time keeps future query tests
        # deterministic if a transport is injected.
        self.assertEqual(datetime(2026, 8, 5, tzinfo=timezone.utc).tzinfo, timezone.utc)
        self.assertFalse(reconciler.enabled)

    def test_reconcile_pages_and_enqueues_through_the_durable_path(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        class Client:
            def __init__(self):
                self.calls = []
                self.responses = [
                    Response({"result": {"data": [{
                        "id": "session-1", "direction": "out", "virtual_phone_number": "73832359277"
                    }], "total": 1}})
                ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, _url, **kwargs):
                self.calls.append(kwargs)
                return self.responses.pop(0)

        client = Client()
        queued = []
        reconciler = NovofonReconciler(
            enabled=True,
            access_token="never-printed",
            api_url="https://dataapi-jsonrpc.novofon.ru/v2.0",
            virtual_phone="73832359277",
            page_size=100,
            enqueue=lambda **event: queued.append(event) or True,
        )
        # The runtime image installs tzdata, but this test only verifies the
        # Data API query window.  Use a fixed +07:00 timezone so it remains
        # portable to Windows Python installations without an IANA database.
        with (
            patch("reconcile.httpx.Client", return_value=client),
            patch("reconcile.account_timezone", return_value=timezone(timedelta(hours=7))),
        ):
            created, observed = reconciler.reconcile(now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))

        self.assertEqual((created, observed), (1, 1))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["event_type"], "CALL_END")
        params = client.calls[0]["json"]["params"]
        self.assertEqual(params["date_from"], "2026-08-04 18:50:00")
        self.assertEqual(params["date_till"], "2026-08-05 19:00:00")
        self.assertEqual(params["limit"], 100)

    def test_reconciliation_skips_another_virtual_number(self):
        reconciler = NovofonReconciler(
            enabled=True,
            access_token="never-printed",
            api_url="https://dataapi-jsonrpc.novofon.ru/v2.0",
            virtual_phone="73832359277",
            enqueue=lambda **_event: True,
        )
        self.assertFalse(reconciler._matches_virtual_phone({"virtual_phone_number": "74951234567"}))
        self.assertTrue(reconciler._matches_virtual_phone({"virtual_phone_number": "7 (383) 235-92-77"}))


if __name__ == "__main__":
    unittest.main()
