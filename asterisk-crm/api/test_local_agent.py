"""Handler-level contract tests for the text-only local-agent ingress."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")
os.environ["LOCAL_AGENT_TOKEN"] = "t" * 32
os.environ["LOCAL_AGENT_ANALYSIS_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def payload(**overrides):
    value = {
        "call_session_id": "session-42",
        "audio_sha256": "a" * 64,
        "audio_duration_sec": 42,
        "asr_model": "gigaam-v3-e2e-rnnt",
        "language": "ru",
        "text": "Тестовый текст",
        "segments": [
            {
                "ordinal": 0,
                "started_ms": 0,
                "ended_ms": 42_000,
                "role": "unknown",
                "speaker_label": "Спикер 1",
                "text": "Тестовый текст",
            }
        ],
    }
    value.update(overrides)
    return app.LocalAgentTranscript(**value)


class Cursor:
    def __init__(self):
        self.rows = [{"version": 1}, {"id": "transcript-1"}]
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def executemany(self, sql, params):
        self.executed.append((sql, list(params)))

    def fetchone(self):
        return self.rows.pop(0)


class Connection:
    def __init__(self):
        self.cursor_value = Cursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True


class Pool:
    def __init__(self):
        self.connection_value = Connection()

    def connection(self):
        return self.connection_value


class LocalAgentIngressTests(unittest.TestCase):
    def test_disabled_or_bad_token_is_rejected_before_any_lookup(self):
        with patch.object(app, "LOCAL_AGENT_TOKEN", ""):
            with self.assertRaises(HTTPException) as disabled:
                app.ingest_local_agent_transcript(payload(), "t" * 32)
        self.assertEqual(disabled.exception.status_code, 503)

        with self.assertRaises(HTTPException) as bad_token:
            app.ingest_local_agent_transcript(payload(), "wrong")
        self.assertEqual(bad_token.exception.status_code, 401)

    def test_unknown_novofon_session_is_not_guessed(self):
        with patch.object(app, "fetch_one", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                app.ingest_local_agent_transcript(payload(), "t" * 32)
        self.assertEqual(caught.exception.status_code, 404)

    def test_duplicate_audio_hash_is_idempotent(self):
        with patch.object(app, "fetch_one", side_effect=[{"id": 8}, {"id": "existing", "version": 4}]):
            result = app.ingest_local_agent_transcript(payload(), "t" * 32)
        self.assertEqual(result, {"call_id": 8, "transcript_id": "existing", "version": 4, "idempotent": True})

    def test_new_audio_hash_stores_text_only_by_default(self):
        fake_pool = Pool()
        with patch.object(app, "fetch_one", side_effect=[{"id": 8}, None]), patch.object(app, "pool", fake_pool):
            result = app.ingest_local_agent_transcript(payload(), "t" * 32)
        self.assertFalse(result["idempotent"])
        statements = "\n".join(sql for sql, _ in fake_pool.connection_value.cursor_value.executed)
        self.assertIn("transcript_segments", statements)
        self.assertNotIn("processing_jobs", statements)
        self.assertNotIn("recordings", statements)
        self.assertNotIn("UPDATE deals", statements)
        self.assertNotIn("UPDATE contacts", statements)
        self.assertNotIn("INSERT INTO tasks", statements)
        call_update = next(params for sql, params in fake_pool.connection_value.cursor_value.executed if "UPDATE calls SET transcript" in sql)
        self.assertEqual(call_update[1], "ready")

    def test_analysis_queue_requires_explicit_enablement(self):
        fake_pool = Pool()
        with (
            patch.object(app, "LOCAL_AGENT_ANALYSIS_ENABLED", True),
            patch.object(app, "fetch_one", side_effect=[{"id": 8}, None]),
            patch.object(app, "pool", fake_pool),
        ):
            app.ingest_local_agent_transcript(payload(), "t" * 32)
        statements = "\n".join(sql for sql, _ in fake_pool.connection_value.cursor_value.executed)
        self.assertIn("processing_jobs", statements)
        self.assertIn("'analyze'", statements)
        call_update = next(params for sql, params in fake_pool.connection_value.cursor_value.executed if "UPDATE calls SET transcript" in sql)
        self.assertEqual(call_update[1], "analyzing")

    def test_novofon_analysis_retry_requires_flag_and_local_transcript(self):
        call = {"id": 8, "source": "novofon", "owner_id": None}
        with patch.object(app, "fetch_one", return_value=call), patch.object(app, "require_call_access"):
            with self.assertRaises(HTTPException) as disabled:
                app.retry_call(8, "analyze", object())
        self.assertEqual(disabled.exception.status_code, 409)

        with (
            patch.object(app, "LOCAL_AGENT_ANALYSIS_ENABLED", True),
            patch.object(app, "fetch_one", side_effect=[call, None]),
            patch.object(app, "require_call_access"),
        ):
            with self.assertRaises(HTTPException) as missing:
                app.retry_call(8, "analyze", object())
        self.assertEqual(missing.exception.status_code, 409)

        statements = []
        with (
            patch.object(app, "LOCAL_AGENT_ANALYSIS_ENABLED", True),
            patch.object(app, "fetch_one", side_effect=[call, {"id": "local-transcript"}]),
            patch.object(app, "require_call_access"),
            patch.object(app, "execute", side_effect=lambda sql, params=(): statements.append((sql, params))),
            patch.object(app, "audit"),
        ):
            self.assertEqual(app.retry_call(8, "analyze", object()), {"queued": True})
        self.assertIn("processing_jobs", statements[0][0])
        self.assertEqual(statements[1][1][0], "analyzing")

    def test_contract_refuses_audio_browser_data_and_invalid_order(self):
        with self.assertRaises(ValidationError):
            payload(audio_url="https://provider.invalid/private", browser_cookie="secret")
        with self.assertRaises(ValidationError):
            payload(segments=[{**payload().segments[0].model_dump(), "ordinal": 1}])
        with self.assertRaises(ValidationError):
            payload(segments=[
                {**payload().segments[0].model_dump(), "ordinal": 0, "started_ms": 0, "ended_ms": 21_000},
                {**payload().segments[0].model_dump(), "ordinal": 1, "started_ms": 20_999, "ended_ms": 42_000},
            ])
        with self.assertRaises(ValidationError):
            payload(audio_duration_sec=40)


if __name__ == "__main__":
    unittest.main()
