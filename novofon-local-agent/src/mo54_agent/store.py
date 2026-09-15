from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .errors import AgentError


DOWNLOADABLE = {"discovered", "retry"}
TRANSCRIBABLE = {"downloaded", "asr_retry"}


@dataclass(frozen=True)
class CallRecord:
    call_session_id: str
    started_at: datetime
    duration_sec: int | None
    status: str
    audio_path: Path | None
    audio_sha256: str | None
    audio_duration_sec: int | None
    transcript_path: Path | None
    attempts: int


class AgentStore:
    """SQLite source of truth for every local processing state."""

    def __init__(self, database: Path):
        self.database = database

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    duration_sec INTEGER,
                    status TEXT NOT NULL CHECK(status IN (
                        'discovered','downloading','downloaded','asr_running','transcribed',
                        'sent','retry','asr_retry','failed','blocked')),
                    audio_path TEXT,
                    audio_sha256 TEXT,
                    audio_duration_sec INTEGER,
                    downloaded_at TEXT,
                    transcribed_at TEXT,
                    sent_at TEXT,
                    transcript_path TEXT,
                    asr_model TEXT,
                    language TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS calls_status_idx ON calls(status, started_at DESC);
                CREATE INDEX IF NOT EXISTS calls_downloaded_idx ON calls(transcribed_at, status)
                    WHERE audio_path IS NOT NULL;
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    event_code TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _record(row: sqlite3.Row) -> CallRecord:
        return CallRecord(
            call_session_id=row["call_session_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            duration_sec=row["duration_sec"],
            status=row["status"],
            audio_path=Path(row["audio_path"]) if row["audio_path"] else None,
            audio_sha256=row["audio_sha256"],
            audio_duration_sec=row["audio_duration_sec"],
            transcript_path=Path(row["transcript_path"]) if row["transcript_path"] else None,
            attempts=row["attempts"],
        )

    def record_inventory(self, call_session_id: str, started_at: datetime, duration_sec: int | None, since: datetime) -> bool:
        if not call_session_id or started_at.tzinfo is None:
            raise ValueError("call_session_id and timezone-aware started_at are required")
        if started_at < since:
            return False
        now = self._now()
        with self._connection() as conn:
            result = conn.execute(
                """INSERT INTO calls(call_session_id,started_at,duration_sec,status,created_at,updated_at)
                   VALUES(?,?,?,'discovered',?,?) ON CONFLICT(call_session_id) DO NOTHING""",
                (call_session_id, started_at.astimezone(timezone.utc).isoformat(), duration_sec, now, now),
            )
            return result.rowcount == 1

    def get(self, call_session_id: str) -> CallRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM calls WHERE call_session_id=?", (call_session_id,)).fetchone()
            return self._record(row) if row else None

    def claim_download(self, call_session_id: str, daily_limit: int, today: date) -> bool:
        """Atomically claim work and enforce the per-day limit on first downloads."""
        with self._connection() as conn:
            row = conn.execute("SELECT status FROM calls WHERE call_session_id=?", (call_session_id,)).fetchone()
            if not row or row["status"] not in DOWNLOADABLE:
                return False
            today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc).isoformat()
            tomorrow_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)
            count = conn.execute(
                "SELECT count(*) FROM calls WHERE downloaded_at>=? AND downloaded_at<?",
                (today_start, tomorrow_start.isoformat()),
            ).fetchone()[0]
            if count >= daily_limit:
                return False
            result = conn.execute(
                """UPDATE calls SET status='downloading', attempts=attempts+1, updated_at=?
                   WHERE call_session_id=? AND status IN ('discovered','retry')""",
                (self._now(), call_session_id),
            )
            return result.rowcount == 1

    def mark_downloaded(self, call_session_id: str, audio_path: Path, sha256: str, duration_sec: int) -> None:
        with self._connection() as conn:
            result = conn.execute(
                """UPDATE calls SET status='downloaded',audio_path=?,audio_sha256=?,audio_duration_sec=?,
                   downloaded_at=?,last_error_code=NULL,updated_at=? WHERE call_session_id=? AND status='downloading'""",
                (str(audio_path), sha256, duration_sec, self._now(), self._now(), call_session_id),
            )
            if result.rowcount != 1:
                raise AgentError("state_conflict", "Download completion no longer owns this call", retryable=True)

    def claim_transcription(self, call_session_id: str) -> bool:
        with self._connection() as conn:
            result = conn.execute(
                """UPDATE calls SET status='asr_running', attempts=attempts+1, updated_at=?
                   WHERE call_session_id=? AND status IN ('downloaded','asr_retry')""",
                (self._now(), call_session_id),
            )
            return result.rowcount == 1

    def mark_transcribed(self, call_session_id: str, transcript_path: Path, asr_model: str, language: str) -> None:
        with self._connection() as conn:
            result = conn.execute(
                """UPDATE calls SET status='transcribed',transcript_path=?,asr_model=?,language=?,
                   transcribed_at=?,last_error_code=NULL,updated_at=? WHERE call_session_id=? AND status='asr_running'""",
                (str(transcript_path), asr_model, language, self._now(), self._now(), call_session_id),
            )
            if result.rowcount != 1:
                raise AgentError("state_conflict", "ASR completion no longer owns this call", retryable=True)

    def mark_sent(self, call_session_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """UPDATE calls SET status='sent',sent_at=?,last_error_code=NULL,updated_at=?
                   WHERE call_session_id=? AND status='transcribed'""",
                (self._now(), self._now(), call_session_id),
            )

    def mark_error(self, call_session_id: str, code: str, *, asr: bool = False, blocked: bool = False) -> None:
        target = "blocked" if blocked else "asr_retry" if asr else "retry"
        with self._connection() as conn:
            conn.execute(
                """UPDATE calls SET status=?,last_error_code=?,updated_at=?
                   WHERE call_session_id=? AND status NOT IN ('sent','transcribed')""",
                (target, code[:100], self._now(), call_session_id),
            )

    def block_pending_downloads(self, code: str) -> int:
        """Explicitly exclude discovered/retry calls without deleting their audit row."""
        with self._connection() as conn:
            result = conn.execute(
                """UPDATE calls SET status='blocked',last_error_code=?,updated_at=?
                   WHERE status IN ('discovered','retry')""",
                (code[:100], self._now()),
            )
            return result.rowcount

    def pending(self, statuses: set[str]) -> list[CallRecord]:
        placeholders = ",".join("?" for _ in statuses)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM calls WHERE status IN ({placeholders}) ORDER BY started_at ASC", tuple(sorted(statuses))
            ).fetchall()
            return [self._record(row) for row in rows]

    def retention_candidates(self, before: datetime) -> list[CallRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM calls WHERE status='sent' AND audio_path IS NOT NULL
                   AND transcribed_at IS NOT NULL AND transcribed_at < ? ORDER BY transcribed_at ASC""",
                (before.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
            return [self._record(row) for row in rows]

    def forget_audio(self, call_session_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE calls SET audio_path=NULL,updated_at=? WHERE call_session_id=? AND status IN ('transcribed','sent')",
                (self._now(), call_session_id),
            )

    def event(self, code: str) -> None:
        with self._connection() as conn:
            conn.execute("INSERT INTO run_events(created_at,event_code) VALUES(?,?)", (self._now(), code[:100]))

    def summary(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute("SELECT status,count(*) AS count FROM calls GROUP BY status").fetchall()
            return {row["status"]: row["count"] for row in rows}


class AgentLock:
    """A one-byte advisory lock: schedule overlap never runs two browser sessions."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "AgentLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        if not self.handle.read(1):
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise AgentError("agent_already_running", "Another agent process is already running", retryable=True) from exc
        return self

    def __exit__(self, *_exc) -> None:
        if not self.handle:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
