import os
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")

if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.Json = lambda value: value
    extras.RealDictCursor = object
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

from db import latest_transcript


class Cursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return {"id": "transcript-7", "text": "canonical transcript", "version": 1}

    def fetchall(self):
        return [
            {"speaker": "manager", "started_ms": 0, "ended_ms": 1200,
             "text": "Hello", "ordinal": 0},
            {"speaker": "customer", "started_ms": 1200, "ended_ms": 2600,
             "text": "Need help", "ordinal": 1},
        ]


class Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def cursor(self, **_kwargs):
        return self.cursor_value


class LatestTranscriptTimelineTests(unittest.TestCase):
    def test_returns_ordered_persisted_segments_for_llm_evidence_validation(self):
        cursor = Cursor()

        @contextmanager
        def fake_connection():
            yield Connection(cursor)

        with patch("db.connection", fake_connection):
            transcript = latest_transcript(42)

        self.assertEqual(transcript["text"], "canonical transcript")
        self.assertEqual(transcript["segments"], [
            {"speaker": "manager", "started_ms": 0, "ended_ms": 1200,
             "text": "Hello", "ordinal": 0},
            {"speaker": "customer", "started_ms": 1200, "ended_ms": 2600,
             "text": "Need help", "ordinal": 1},
        ])
        self.assertIn("ORDER BY ordinal", cursor.executed[1][0])
        self.assertEqual(cursor.executed[1][1], ("transcript-7",))


if __name__ == "__main__":
    unittest.main()
