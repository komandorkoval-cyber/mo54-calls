from __future__ import annotations

import json
import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .asr import LocalASR, Transcript
from .browser import DownloadedAudio, NovofonBrowser
from .config import AgentConfig, AgentPaths
from .crm import CRMClient
from .errors import AgentError
from .observability import notify, prevent_sleep, safe_logger
from .store import AgentStore


class AgentRunner:
    def __init__(self, paths: AgentPaths, config: AgentConfig, store: AgentStore):
        self.paths = paths
        self.config = config
        self.store = store
        self.log = safe_logger(paths.log)

    def _disk_ok(self) -> bool:
        usage = shutil.disk_usage(self.paths.root)
        free_gib = usage.free / 1024**3
        free_percent = usage.free * 100 / usage.total
        return free_gib >= self.config.minimum_free_gib and free_percent >= self.config.minimum_free_percent

    def purge_audio(self) -> bool:
        """Delete only audio whose transcript delivery to CRM was confirmed."""
        before = datetime.now(timezone.utc) - timedelta(days=self.config.retention_days)
        for record in self.store.retention_candidates(before):
            if record.audio_path and record.audio_path.is_file():
                try:
                    record.audio_path.unlink()
                except OSError:
                    continue
            self.store.forget_audio(record.call_session_id)
        if self._disk_ok():
            return True
        # Low-space mode may shorten retention, but preserves all unfinished or
        # undelivered recordings.
        for record in self.store.retention_candidates(datetime.now(timezone.utc) + timedelta(days=1)):
            if self._disk_ok():
                return True
            if record.audio_path and record.audio_path.is_file():
                try:
                    record.audio_path.unlink()
                except OSError:
                    continue
            self.store.forget_audio(record.call_session_id)
        return self._disk_ok()

    def _save_transcript(self, call_session_id: str, transcript: Transcript) -> Path:
        # A provider ID can contain characters valid for an API but invalid in a
        # Windows filename, so no provider ID is placed in a local filename.
        safe_name = hashlib.sha256(call_session_id.encode("utf-8")).hexdigest()
        path = self.paths.transcripts / f"{safe_name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return path

    def _download_audio(self, call_session_id: str) -> DownloadedAudio:
        browser = NovofonBrowser(self.paths, self.config)
        with prevent_sleep():
            return browser.download(call_session_id)

    def _transcribe(self, call_session_id: str, audio_path: Path) -> Transcript:
        asr = LocalASR(self.paths, self.config)
        with prevent_sleep():
            return asr.transcribe(audio_path)

    def download_pilot(self, call_session_id: str) -> DownloadedAudio:
        """Download one inventoried recent call and stop before ASR or CRM.

        This explicit path avoids `run-once` during acceptance: it cannot
        discover, transcribe, or deliver any other call as a side effect.
        """
        record = self.store.get(call_session_id)
        if not record:
            raise AgentError("pilot_call_not_in_inventory", "Run local inventory before selecting a pilot call", retryable=False)
        if record.started_at < datetime.now(timezone.utc) - timedelta(days=7):
            raise AgentError("pilot_call_outside_window", "The selected pilot call is outside the seven-day window", retryable=False)
        if record.status not in {"discovered", "retry"}:
            raise AgentError("pilot_call_not_downloadable", "The selected pilot call is not ready for one-time download", retryable=False)
        if not self._disk_ok():
            raise AgentError("disk_space_critical", "Local disk space is below the configured safety threshold", retryable=False)
        if not self.store.claim_download(call_session_id, self.config.daily_download_limit, datetime.now(timezone.utc).date()):
            raise AgentError("pilot_download_limit_or_state", "The daily limit or call state blocks this pilot download", retryable=False)
        try:
            audio = self._download_audio(call_session_id)
            self.store.mark_downloaded(call_session_id, audio.path, audio.sha256, audio.duration_sec)
            return audio
        except AgentError as exc:
            self.store.mark_error(call_session_id, exc.code)
            self.store.event(exc.code)
            self.log.warning("pilot_download_failed code=%s", exc.code)
            raise

    def run_once(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        browser = NovofonBrowser(self.paths, self.config)
        discovered = downloaded = transcribed = delivered = 0
        try:
            for item in browser.inventory():
                if self.store.record_inventory(item.call_session_id, item.started_at, item.duration_sec, since):
                    discovered += 1
        except AgentError as exc:
            self.store.event(exc.code)
            self.log.warning("inventory_failed code=%s", exc.code)
            notify("MO54 Calls Agent", exc.code)
            return {"discovered": discovered, "downloaded": 0, "transcribed": 0, "delivered": 0}

        if not self.purge_audio():
            self.store.event("disk_space_critical")
            self.log.warning("downloads_blocked code=disk_space_critical")
            notify("MO54 Calls Agent", "disk_space_critical")
            return {"discovered": discovered, "downloaded": 0, "transcribed": 0, "delivered": 0}

        for record in self.store.pending({"discovered", "retry"}):
            if not self.store.claim_download(record.call_session_id, self.config.daily_download_limit, now.date()):
                continue
            try:
                audio = self._download_audio(record.call_session_id)
                self.store.mark_downloaded(record.call_session_id, audio.path, audio.sha256, audio.duration_sec)
                downloaded += 1
            except AgentError as exc:
                self.store.mark_error(record.call_session_id, exc.code)
                self.store.event(exc.code)
                self.log.warning("download_failed code=%s", exc.code)
                notify("MO54 Calls Agent", exc.code)

        for record in self.store.pending({"downloaded", "asr_retry"}):
            if not record.audio_path or not record.audio_path.is_file() or not self.store.claim_transcription(record.call_session_id):
                continue
            try:
                transcript = self._transcribe(record.call_session_id, record.audio_path)
                transcript_path = self._save_transcript(record.call_session_id, transcript)
                self.store.mark_transcribed(record.call_session_id, transcript_path, transcript.asr_model, transcript.language)
                transcribed += 1
            except AgentError as exc:
                self.store.mark_error(record.call_session_id, exc.code, asr=True)
                self.store.event(exc.code)
                self.log.warning("asr_failed code=%s", exc.code)
                notify("MO54 Calls Agent", exc.code)

        client = CRMClient(self.config)
        for record in self.store.pending({"transcribed"}):
            # Delivery needs the persisted hash/duration and local transcript,
            # not the audio file itself. It can therefore recover after the
            # permitted 21-day audio retention has elapsed.
            if not record.audio_sha256 or not record.audio_duration_sec or not record.transcript_path:
                continue
            try:
                transcript = Transcript.from_path(record.transcript_path)
                metadata_path = record.audio_path or self.paths.audio / "retained-locally-no-longer-present"
                client.send_transcript(record.call_session_id, DownloadedAudio(metadata_path, record.audio_sha256, record.audio_duration_sec), transcript)
                self.store.mark_sent(record.call_session_id)
                delivered += 1
            except AgentError as exc:
                # Keep status=transcribed so that delivery can be retried; never redo ASR/download.
                self.store.event(exc.code)
                self.log.warning("crm_delivery_failed code=%s", exc.code)
                notify("MO54 Calls Agent", exc.code)
        return {"discovered": discovered, "downloaded": downloaded, "transcribed": transcribed, "delivered": delivered}
