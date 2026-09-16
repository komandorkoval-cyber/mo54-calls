from __future__ import annotations

import json
import http.client
import io
from hashlib import sha256
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mo54_agent import cli
from mo54_agent.config import AgentConfig, AgentPaths
from mo54_agent.runner import AgentRunner
from mo54_agent.errors import AgentError
from mo54_agent.review import (
    automatic_baseline_path,
    build_reviewed_transcript,
    create_approved_review,
    load_approved_review,
    parse_tagged_transcript,
    review_artifact_path,
    _ReviewContext,
    _ReviewHTTPServer,
    _default_review_text,
    _review_speech_bounds,
)
from mo54_agent.store import AgentStore, CallRecord


class PilotReviewTests(unittest.TestCase):
    @staticmethod
    def _run_cli(argv: list[str]) -> None:
        # The command deliberately emits only delivery metadata.  Suppress it
        # here so test output cannot become an accidental operator log.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(argv)

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
        self.source = self.paths.transcripts / "asr.json"
        self.source.write_text(json.dumps({
            "text": "[Спикер 1]: Алло\n[Спикер 2]: Здравствуйте",
            "asr_model": "gigaam-v3-e2e-rnnt",
            "language": "ru",
            "segments": [
                {"ordinal": 0, "started_ms": 0, "ended_ms": 900, "role": "unknown", "text": "Алло", "speaker_label": "Спикер 1"},
                {"ordinal": 1, "started_ms": 1000, "ended_ms": 2500, "role": "unknown", "text": "Здравствуйте", "speaker_label": "Спикер 2"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        self.audio = self.paths.audio / "recording.m4a"
        self.audio.write_bytes(b"local audio only")
        self.audio_sha256 = sha256(self.audio.read_bytes()).hexdigest()
        self.review_seal_key = b"r" * 32
        self.create_seal_key = patch(
            "mo54_agent.review.get_or_create_review_seal_key", return_value=self.review_seal_key
        )
        self.load_seal_key = patch(
            "mo54_agent.review.get_review_seal_key", return_value=self.review_seal_key
        )
        self.create_seal_key.start()
        self.load_seal_key.start()
        self.record = CallRecord(
            call_session_id="pilot-42",
            started_at=datetime.now(timezone.utc),
            duration_sec=3,
            status="transcribed",
            audio_path=self.audio,
            audio_sha256=self.audio_sha256,
            audio_duration_sec=3,
            transcript_path=self.source,
            attempts=1,
        )

    def tearDown(self) -> None:
        self.load_seal_key.stop()
        self.create_seal_key.stop()
        self.temp.cleanup()

    def test_parser_accepts_only_operator_labels_and_continuations(self) -> None:
        turns = parse_tagged_transcript("[я]: Добрый\nдень\n[Клиент]: Здравствуйте")

        self.assertEqual([(turn.role, turn.text) for turn in turns], [
            ("manager", "Добрый день"),
            ("customer", "Здравствуйте"),
        ])
        with self.assertRaises(AgentError) as invalid:
            parse_tagged_transcript("[Спикер 1]: Текст")
        self.assertEqual(invalid.exception.code, "pilot_review_tag_invalid")

    def test_timing_validation_rejects_overlap_and_audio_overflow(self) -> None:
        transcript = build_reviewed_transcript(
            "[я]: Алло\n[Клиент]: Здравствуйте",
            [{"started_ms": "0", "ended_ms": "800"}, {"started_ms": "800", "ended_ms": "2000"}],
            audio_duration_sec=3,
            asr_model="test",
            language="ru",
        )
        self.assertEqual([segment.role for segment in transcript.segments], ["manager", "customer"])
        self.assertEqual(transcript.text, "[я]: Алло\n[Клиент]: Здравствуйте")

        with self.assertRaises(AgentError) as overlap:
            build_reviewed_transcript(
                "[я]: Алло\n[Клиент]: Здравствуйте",
                [{"started_ms": 0, "ended_ms": 1200}, {"started_ms": 1000, "ended_ms": 2000}],
                audio_duration_sec=3,
                asr_model="test",
                language="ru",
            )
        self.assertEqual(overlap.exception.code, "pilot_review_timing_overlap")

        with self.assertRaises(AgentError) as overflow:
            build_reviewed_transcript(
                "[я]: Алло",
                [{"started_ms": 0, "ended_ms": 3001}],
                audio_duration_sec=3,
                asr_model="test",
                language="ru",
            )
        self.assertEqual(overflow.exception.code, "pilot_review_timing_out_of_bounds")

    def test_keyboard_editor_boundary_round_trips_through_local_approval(self) -> None:
        """The simplified editor still saves only real, ordered playhead boundaries."""
        context = _ReviewContext(self.paths, self.record, "local draft", 0, 3000)
        server = _ReviewHTTPServer(("127.0.0.1", 0), context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({
                "tagged_text": "[Клиент]: Здравствуйте\n[я]: Алло",
                "timings": [
                    {"started_ms": 0, "ended_ms": 1250},
                    {"started_ms": 1250, "ended_ms": 3000},
                ],
            }, ensure_ascii=False).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(
                "POST",
                "/approve",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{server.server_port}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            transcript = load_approved_review(self.paths, self.record)
            self.assertEqual([segment.role for segment in transcript.segments], ["customer", "manager"])
            self.assertEqual(
                [(segment.started_ms, segment.ended_ms) for segment in transcript.segments],
                [(0, 1250), (1250, 3000)],
            )
        finally:
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()

    def test_keyboard_editor_uses_neutral_draft_and_observed_speech_envelope(self) -> None:
        automatic = build_reviewed_transcript(
            "[я]: Алло\n[Клиент]: Здравствуйте",
            [{"started_ms": 500, "ended_ms": 900}, {"started_ms": 1100, "ended_ms": 2500}],
            audio_duration_sec=3,
            asr_model="test",
            language="ru",
        )
        self.assertEqual(_default_review_text(automatic), "Алло Здравствуйте")
        self.assertEqual(_review_speech_bounds(automatic, 3), (500, 2500))

    def test_approved_review_keeps_auto_source_separate_and_verifiable(self) -> None:
        artifact = create_approved_review(
            self.paths,
            self.record,
            tagged_text="[я]: Алло\n[Клиент]: Здравствуйте",
            timings=[{"started_ms": 0, "ended_ms": 900}, {"started_ms": 1000, "ended_ms": 2500}],
        )

        self.assertTrue(automatic_baseline_path(self.source).is_file())
        self.assertEqual(artifact, review_artifact_path(self.paths, "pilot-42"))
        self.assertNotEqual(artifact, self.source)
        raw = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(raw["state"], "approved")
        self.assertEqual(raw["seal"]["algorithm"], "hmac-sha256")
        self.assertIn("word_error_rate", raw["metrics"])
        self.assertTrue(raw["metrics"]["automatic_role_metrics_available"])

        transcript = load_approved_review(self.paths, self.record)
        self.assertEqual([segment.role for segment in transcript.segments], ["manager", "customer"])
        self.assertEqual(transcript.segments[1].started_ms, 1000)
        self.assertEqual(transcript.asr_model, "operator-reviewed:gigaam-v3-e2e-rnnt")

        raw["review"]["tagged_text"] = "[я]: Подмена"
        raw["review_sha256"] = sha256(
            json.dumps(raw["review"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        artifact.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(AgentError) as changed:
            load_approved_review(self.paths, self.record)
        self.assertEqual(changed.exception.code, "pilot_review_seal_invalid")

    def test_review_approval_rejects_audio_with_a_different_hash(self) -> None:
        self.audio.write_bytes(b"tampered local audio")
        with self.assertRaises(AgentError) as invalid:
            create_approved_review(
                self.paths,
                self.record,
                tagged_text="[я]: Алло",
                timings=[{"started_ms": 0, "ended_ms": 900}],
            )
        self.assertEqual(invalid.exception.code, "pilot_review_audio_hash_mismatch")

    def test_delivery_loader_blocks_without_an_approved_review(self) -> None:
        with self.assertRaises(AgentError) as missing:
            load_approved_review(self.paths, self.record)
        self.assertEqual(missing.exception.code, "pilot_review_missing")

    def test_scheduled_delivery_is_deferred_until_the_local_review_is_approved(self) -> None:
        store = AgentStore(self.paths.database)
        store.initialize()
        now = datetime.now(timezone.utc)
        self.assertTrue(store.record_inventory("pilot-42", now, 3, now.replace(year=now.year - 1)))
        self.assertTrue(store.claim_download("pilot-42", 10, now.date()))
        store.mark_downloaded("pilot-42", self.audio, self.audio_sha256, 3)
        self.assertTrue(store.claim_transcription("pilot-42"))
        store.mark_transcribed("pilot-42", self.source, "gigaam-v3-e2e-rnnt", "ru")
        runner = AgentRunner(self.paths, AgentConfig(), store)
        try:
            with patch("mo54_agent.runner.NovofonBrowser.inventory", return_value=[]), patch.object(runner, "purge_audio", return_value=True), patch("mo54_agent.runner.CRMClient.send_transcript") as send:
                result = runner.run_once()
            self.assertEqual(result["delivered"], 0)
            send.assert_not_called()
            self.assertEqual(store.get("pilot-42").status, "transcribed")
        finally:
            for handler in list(runner.log.handlers):
                runner.log.removeHandler(handler)
                handler.close()

    def test_cli_blocks_unapproved_delivery_and_repeats_without_a_second_send(self) -> None:
        """The pilot command itself, not only its loader, enforces review immutability."""
        store = AgentStore(self.paths.database)
        store.initialize()
        now = datetime.now(timezone.utc)
        self.assertTrue(store.record_inventory("pilot-42", now, 3, now.replace(year=now.year - 1)))
        self.assertTrue(store.claim_download("pilot-42", 10, now.date()))
        store.mark_downloaded("pilot-42", self.audio, self.audio_sha256, 3)
        self.assertTrue(store.claim_transcription("pilot-42"))
        store.mark_transcribed("pilot-42", self.source, "gigaam-v3-e2e-rnnt", "ru")
        config = AgentConfig(crm_url="https://calls.example.invalid")

        with patch("mo54_agent.cli.AgentPaths.default", return_value=self.paths), patch(
            "mo54_agent.cli.AgentConfig.load", return_value=config
        ):
            with self.assertRaises(SystemExit) as blocked:
                self._run_cli(["deliver-pilot", "pilot-42"])
        self.assertEqual(blocked.exception.code, 2)

        create_approved_review(
            self.paths,
            store.get("pilot-42"),
            tagged_text="[я]: Алло\n[Клиент]: Здравствуйте",
            timings=[{"started_ms": 0, "ended_ms": 900}, {"started_ms": 1000, "ended_ms": 2500}],
        )
        with patch("mo54_agent.cli.AgentPaths.default", return_value=self.paths), patch(
            "mo54_agent.cli.AgentConfig.load", return_value=config
        ), patch("mo54_agent.cli.CRMClient.send_transcript", return_value={"idempotent": False}) as send:
            self._run_cli(["deliver-pilot", "pilot-42"])
            send.assert_called_once()

        self.assertEqual(store.get("pilot-42").status, "sent")
        with patch("mo54_agent.cli.AgentPaths.default", return_value=self.paths), patch(
            "mo54_agent.cli.AgentConfig.load", return_value=config
        ), patch("mo54_agent.cli.CRMClient.send_transcript") as duplicate_send:
            self._run_cli(["deliver-pilot", "pilot-42"])
            duplicate_send.assert_not_called()

    def test_editor_is_loopback_only_and_can_cancel_without_saving(self) -> None:
        automatic = build_reviewed_transcript(
            "[я]: Алло\n[Клиент]: Здравствуйте",
            [{"started_ms": 0, "ended_ms": 900}, {"started_ms": 1000, "ended_ms": 2500}],
            audio_duration_sec=3,
            asr_model="test",
            language="ru",
        )
        context = _ReviewContext(self.paths, self.record, _default_review_text(automatic))
        server = _ReviewHTTPServer(("127.0.0.1", 0), context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/")
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
            self.assertIn('data-testid="first-speaker"', page)
            self.assertIn("splitTurnAtPlayhead", page)
            self.assertIn("Shift+Enter", page)
            self.assertNotIn("Взять с плеера", page)
            self.assertNotIn("name='started_ms'", page)

            connection.request("POST", "/cancel", headers={"Origin": "https://untrusted.example"})
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 403)
            rejected.read()

            expected_origin = f"http://127.0.0.1:{server.server_port}"
            connection.request("POST", "/cancel", headers={"Origin": expected_origin})
            cancelled = connection.getresponse()
            self.assertEqual(cancelled.status, 200)
            cancelled.read()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(context.approved_path)
        finally:
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
