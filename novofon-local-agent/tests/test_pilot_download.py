from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mo54_agent.browser import DownloadedAudio
from mo54_agent.errors import AgentError
from mo54_agent.runner import AgentRunner
from mo54_agent.store import AgentStore


class PilotDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = AgentStore(root / "agent.sqlite3")
        self.store.initialize()
        self.paths = SimpleNamespace(root=root, log=root / "agent.log")
        self.config = SimpleNamespace(daily_download_limit=1)
        self.runner = AgentRunner(self.paths, self.config, self.store)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        # `safe_logger` intentionally reuses one process logger.  Close the
        # test-owned Windows file handle before TemporaryDirectory cleanup.
        for handler in list(self.runner.log.handlers):
            self.runner.log.removeHandler(handler)
            handler.close()
        self.temp.cleanup()

    def test_downloads_only_the_selected_inventoried_call(self) -> None:
        self.assertTrue(self.store.record_inventory("pilot-1", self.now, 10, self.now.replace(year=self.now.year - 1)))
        audio_path = Path(self.temp.name) / "recording.mp3"
        audio_path.write_bytes(b"test audio")
        audio = DownloadedAudio(audio_path, "a" * 64, 10)

        with patch.object(self.runner, "_disk_ok", return_value=True), patch.object(self.runner, "_download_audio", return_value=audio) as download:
            result = self.runner.download_pilot("pilot-1")

        self.assertEqual(result, audio)
        download.assert_called_once_with("pilot-1")
        record = self.store.get("pilot-1")
        self.assertEqual(record.status, "downloaded")
        self.assertEqual(record.audio_sha256, "a" * 64)

    def test_rejects_a_call_that_was_not_inventoried(self) -> None:
        with patch.object(self.runner, "_disk_ok", return_value=True):
            with self.assertRaises(AgentError) as caught:
                self.runner.download_pilot("missing-1")
        self.assertEqual(caught.exception.code, "pilot_call_not_in_inventory")

    def test_duration_tolerance_allows_connection_overhead_but_not_a_wrong_recording(self) -> None:
        self.assertTrue(self.runner.duration_within_inventory_tolerance(203, 189))
        self.assertFalse(self.runner.duration_within_inventory_tolerance(203, 150))


if __name__ == "__main__":
    unittest.main()
