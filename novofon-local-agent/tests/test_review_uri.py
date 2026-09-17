from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from mo54_agent import cli
from mo54_agent.config import AgentConfig, AgentPaths
from mo54_agent.errors import AgentError
from mo54_agent.review import create_approved_review, parse_review_uri
from mo54_agent.review_protocol import review_uri_command
from mo54_agent.store import AgentStore


class ReviewUriTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = AgentPaths(
            root,
            root / "audio",
            root / "staging",
            root / "transcripts",
            root / "models",
            root / "profile",
            root / "agent.sqlite3",
            root / "config.json",
            root / "agent.log",
            root / "agent.lock",
            root / "embedding",
        )
        self.paths.ensure()
        self.store = AgentStore(self.paths.database)
        self.store.initialize()
        self.now = datetime.now(timezone.utc)
        self.session_id = "session:42"
        self.audio = self.paths.audio / "recording.m4a"
        self.audio.write_bytes(b"local review audio")
        self.audio_sha256 = sha256(self.audio.read_bytes()).hexdigest()
        self.source = self.paths.transcripts / "asr.json"
        self.source.write_text(json.dumps({
            "text": "hello",
            "asr_model": "test-asr",
            "language": "ru",
            "segments": [{
                "ordinal": 0,
                "started_ms": 0,
                "ended_ms": 1000,
                "role": "unknown",
                "text": "hello",
                "speaker_label": "Speaker 1",
            }],
        }), encoding="utf-8")
        self.assertTrue(self.store.record_inventory(self.session_id, self.now, 1, self.now - timedelta(days=7)))
        self.assertTrue(self.store.claim_download(self.session_id, 10, self.now.date()))
        self.store.mark_downloaded(self.session_id, self.audio, self.audio_sha256, 1)
        self.assertTrue(self.store.claim_transcription(self.session_id))
        self.store.mark_transcribed(self.session_id, self.source, "test-asr", "ru")
        self.config = AgentConfig(crm_url="https://calls.example.invalid")
        self.review_uri = f"mo54-calls-review://review/{self.session_id}"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            cli.main(argv)
        return output.getvalue(), errors.getvalue()

    def _create_approved_review(self) -> None:
        with patch("mo54_agent.review.get_or_create_review_seal_key", return_value=b"r" * 32):
            create_approved_review(
                self.paths,
                self.store.get(self.session_id),
                tagged_text="[я]: hello",
                timings=None,
            )

    def test_parser_accepts_only_the_exact_registered_shape(self) -> None:
        self.assertEqual(parse_review_uri("mo54-calls-review://review/session%3A42"), "session:42")
        invalid = [
            "https://review/session:42",
            "mo54-calls-review://other/session:42",
            "mo54-calls-review://review/session:42/other",
            "mo54-calls-review://review/session:42?next=anything",
            "mo54-calls-review://review/session:42#anything",
            "mo54-calls-review://review/session%252Fother",
            "mo54-calls-review://review/../../other",
            "mo54-calls-review://review/",
            " mo54-calls-review://review/session:42",
        ]
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(AgentError) as caught:
                parse_review_uri(uri)
            self.assertEqual(caught.exception.code, "review_uri_invalid")

    def test_ready_uri_opens_only_local_review_and_can_defer_delivery(self) -> None:
        with patch("mo54_agent.cli.AgentPaths.default", return_value=self.paths), patch(
            "mo54_agent.cli.AgentConfig.load", return_value=self.config
        ), patch("mo54_agent.cli.review_pilot", return_value=self.paths.reviews / "approved.review.json") as review, patch(
            "mo54_agent.cli._confirm_approved_delivery", return_value=False
        ) as confirm, patch("mo54_agent.cli.CRMClient") as crm, patch("mo54_agent.cli.NovofonBrowser") as browser:
            output, errors = self._run_cli(["review-uri", self.review_uri])

        self.assertEqual(errors, "")
        self.assertIn('"delivery_deferred": true', output)
        review.assert_called_once()
        confirm.assert_called_once()
        crm.assert_not_called()
        browser.assert_not_called()
        self.assertEqual(self.store.get(self.session_id).status, "transcribed")

    def test_approved_undelivered_review_is_reused_and_only_yes_contacts_crm(self) -> None:
        self._create_approved_review()
        with patch("mo54_agent.review.get_review_seal_key", return_value=b"r" * 32), patch(
            "mo54_agent.cli.AgentPaths.default", return_value=self.paths
        ), patch("mo54_agent.cli.AgentConfig.load", return_value=self.config), patch(
            "mo54_agent.cli.review_pilot"
        ) as review, patch("mo54_agent.cli._confirm_approved_delivery", return_value=True), patch(
            "mo54_agent.cli.CRMClient.send_transcript", return_value={"idempotent": False}
        ) as send, patch("mo54_agent.cli.NovofonBrowser") as browser:
            output, errors = self._run_cli(["review-uri", self.review_uri])

        self.assertEqual(errors, "")
        self.assertIn('"delivered": 1', output)
        review.assert_not_called()
        send.assert_called_once()
        browser.assert_not_called()
        self.assertEqual(self.store.get(self.session_id).status, "sent")

    def test_not_ready_uri_shows_a_local_error_without_starting_processing(self) -> None:
        pending_id = "pending-7"
        self.assertTrue(self.store.record_inventory(pending_id, self.now, 1, self.now - timedelta(days=7)))
        uri = f"mo54-calls-review://review/{pending_id}"
        with patch("mo54_agent.cli.AgentPaths.default", return_value=self.paths), patch(
            "mo54_agent.cli.AgentConfig.load", return_value=self.config
        ), patch("mo54_agent.cli._show_review_uri_error") as show_error, patch(
            "mo54_agent.cli.review_pilot"
        ) as review, patch("mo54_agent.cli.CRMClient") as crm, patch("mo54_agent.cli.NovofonBrowser") as browser:
            with self.assertRaises(SystemExit) as stopped:
                self._run_cli(["review-uri", uri])

        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("подготовлен", show_error.call_args.args[0])
        review.assert_not_called()
        crm.assert_not_called()
        browser.assert_not_called()
        self.assertEqual(self.store.get(pending_id).status, "discovered")

    def test_install_review_uri_is_a_narrow_maintenance_command(self) -> None:
        executable = self.paths.root / "venv" / "Scripts" / "mo54-agent.exe"
        with patch("mo54_agent.cli.install_review_uri_handler", return_value=executable) as install, patch(
            "mo54_agent.cli._components"
        ) as components:
            output, errors = self._run_cli(["install-review-uri"])

        self.assertEqual(errors, "")
        self.assertIn('"review_uri_handler_installed": true', output)
        install.assert_called_once_with()
        components.assert_not_called()
        self.assertEqual(
            review_uri_command(executable),
            f'"{executable}" review-uri "%1"',
        )


if __name__ == "__main__":
    unittest.main()
