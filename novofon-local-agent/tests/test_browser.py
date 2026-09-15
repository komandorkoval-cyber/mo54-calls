from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mo54_agent.browser import NovofonBrowser, parse_duration
from mo54_agent.config import AgentConfig, AgentPaths
from mo54_agent.errors import StableIdentifierMissing


class BrowserAdapterTests(unittest.TestCase):
    def _browser(self) -> NovofonBrowser:
        root = Path(tempfile.mkdtemp())
        paths = AgentPaths(root, root / "audio", root / "staging", root / "transcripts", root / "models", root / "profile", root / "db.sqlite", root / "config.json", root / "agent.log", root / "lock", root / "embedding")
        return NovofonBrowser(paths, AgentConfig(novofon_calls_url="https://example.invalid/calls"))

    def test_mock_page_rows_require_explicit_stable_identifier(self) -> None:
        browser = self._browser()
        calls = browser._rows_to_calls([{"session": "session-42", "started": "2026-08-12T10:00:00+07:00", "duration": "00:42"}])
        self.assertEqual(calls[0].call_session_id, "session-42")
        self.assertEqual(calls[0].duration_sec, 42)
        with self.assertRaises(StableIdentifierMissing):
            browser._rows_to_calls([{"session": "", "started": "2026-08-12T10:00:00+07:00", "duration": "00:42"}])

    def test_duration_parser_refuses_unknown_layout(self) -> None:
        self.assertEqual(parse_duration("01:02:03"), 3723)
        self.assertIsNone(parse_duration("about an hour"))
