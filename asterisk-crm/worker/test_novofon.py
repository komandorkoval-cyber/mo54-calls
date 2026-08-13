import unittest

from novofon import parse_event


class NovofonNotificationTests(unittest.TestCase):
    def test_record_notification_without_explicit_type_is_recognized(self):
        event = parse_event(
            {
                "call_session_id": "session-42",
                "virtual_phone_number": "+79991234567",
                "notification_name": "CRM — запись звонка",
                "notification_time": "2026-08-12 10:00:00",
                "file_link": "https://files.novofon.ru/record/42.wav",
            }
        )

        self.assertEqual(event.event_type, "RECORD_CALL")
        self.assertEqual(event.call_session_id, "session-42")
        self.assertEqual(event.recording_url, "https://files.novofon.ru/record/42.wav")


if __name__ == "__main__":
    unittest.main()
