from __future__ import annotations

from .asr import Transcript
from .browser import DownloadedAudio
from .config import AgentConfig
from .errors import AgentError
from .security import get_crm_token


class CRMClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def send_transcript(self, call_session_id: str, audio: DownloadedAudio, transcript: Transcript) -> dict:
        if not self.config.crm_url:
            raise AgentError("crm_url_missing", "CRM URL is not configured", retryable=False)
        payload = {
            "call_session_id": call_session_id,
            "audio_sha256": audio.sha256,
            "audio_duration_sec": audio.duration_sec,
            "asr_model": transcript.asr_model,
            "language": transcript.language,
            "text": transcript.text,
            "segments": [
                {
                    "ordinal": segment.ordinal,
                    "started_ms": segment.started_ms,
                    "ended_ms": segment.ended_ms,
                    "role": segment.role,
                    "speaker_label": segment.speaker_label,
                    "text": segment.text,
                }
                for segment in transcript.segments
            ],
        }
        try:
            import httpx
            response = httpx.post(
                self.config.crm_url.rstrip("/") + "/api/integrations/local-agent/transcripts",
                headers={"X-Local-Agent-Token": get_crm_token()},
                json=payload,
                timeout=30,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise AgentError("crm_delivery_failed", "CRM transcript delivery could not be confirmed", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise AgentError("crm_token_rejected", "CRM rejected local-agent credential", retryable=False)
        if response.status_code == 404:
            raise AgentError("crm_call_not_found", "CRM does not have the matching Novofon call", retryable=True)
        if response.status_code >= 500:
            raise AgentError("crm_delivery_failed", "CRM temporarily cannot accept the transcript", retryable=True)
        if response.status_code >= 400:
            raise AgentError("crm_transcript_rejected", "CRM rejected transcript metadata", retryable=False)
        try:
            return response.json()
        except ValueError as exc:
            raise AgentError("crm_invalid_response", "CRM returned an invalid transcript response", retryable=True) from exc
