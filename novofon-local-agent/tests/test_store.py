from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mo54_agent.store import AgentStore


class AgentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.temp.name) / "agent.sqlite3")
        self.store.initialize()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_inventory_never_adds_call_older_than_seven_days(self) -> None:
        self.assertFalse(self.store.record_inventory("old-1", self.now - timedelta(days=8), 15, self.now - timedelta(days=7)))
        self.assertTrue(self.store.record_inventory("recent-1", self.now - timedelta(days=6), 15, self.now - timedelta(days=7)))
        self.assertFalse(self.store.record_inventory("recent-1", self.now - timedelta(days=6), 15, self.now - timedelta(days=7)))
        self.assertEqual(self.store.get("recent-1").status, "discovered")

    def test_daily_limit_and_retry_do_not_duplicate_call(self) -> None:
        for session in ("call-1", "call-2"):
            self.store.record_inventory(session, self.now, 10, self.now - timedelta(days=7))
        self.assertTrue(self.store.claim_download("call-1", 1, self.now.date()))
        self.store.mark_error("call-1", "download_interrupted")
        self.assertEqual(self.store.get("call-1").status, "retry")
        self.assertTrue(self.store.claim_download("call-1", 1, self.now.date()))
        self.store.mark_downloaded("call-1", Path(self.temp.name) / "sound.wav", "a" * 64, 10)
        self.assertFalse(self.store.claim_download("call-2", 1, self.now.date()))
        self.assertEqual(len(self.store.pending({"downloaded"})), 1)

    def test_only_crm_delivered_audio_is_eligible_for_cleanup(self) -> None:
        self.store.record_inventory("sent-call", self.now, 10, self.now - timedelta(days=7))
        self.store.claim_download("sent-call", 50, self.now.date())
        self.store.mark_downloaded("sent-call", Path(self.temp.name) / "old.wav", "b" * 64, 10)
        self.store.claim_transcription("sent-call")
        self.store.mark_transcribed("sent-call", Path(self.temp.name) / "text.json", "gigaam-v3-e2e-rnnt", "ru")
        self.store.record_inventory("unprocessed", self.now, 10, self.now - timedelta(days=7))
        self.assertEqual(self.store.retention_candidates(self.now + timedelta(days=1)), [])
        self.store.mark_sent("sent-call")
        candidates = self.store.retention_candidates(self.now + timedelta(days=1))
        self.assertEqual([item.call_session_id for item in candidates], ["sent-call"])
        self.store.forget_audio("sent-call")
        self.assertIsNone(self.store.get("sent-call").audio_path)
