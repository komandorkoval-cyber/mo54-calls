from __future__ import annotations

import hmac
import json
import mimetypes
import re
import threading
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from .asr import Transcript, TranscriptSegment
from .errors import AgentError
from .security import get_or_create_review_seal_key, get_review_seal_key

if TYPE_CHECKING:
    from .config import AgentPaths
    from .store import CallRecord


REVIEW_SCHEMA_VERSION = 1
REVIEW_SEAL_ALGORITHM = "hmac-sha256"
REVIEW_SEAL_KEY_ID = "windows-credential-manager-v1"
MAX_REVIEW_REQUEST_BYTES = 2 * 1024 * 1024
TIMING_MODE_MANUAL = "manual_audio_boundary"
TIMING_MODE_TEXT_ONLY = "text_only"
_TAGGED_TURN = re.compile(r"^\s*\[(?P<label>[^\]]+)\]\s*:\s*(?P<text>.*?)\s*$")
_ROLE_FOR_LABEL = {"я": "manager", "клиент": "customer"}
_DISPLAY_LABEL_FOR_ROLE = {"manager": "я", "customer": "Клиент"}


@dataclass(frozen=True)
class ReviewTurn:
    role: str
    label: str
    text: str


def automatic_baseline_path(transcript_path: Path) -> Path:
    """Keep the ASR output separate from any later operator correction."""
    return transcript_path.with_name(f"{transcript_path.stem}.automatic.json")


def preserve_automatic_baseline(transcript_path: Path) -> Path:
    """Make a one-time immutable source snapshot before a manual edit."""
    if not transcript_path.is_file():
        raise AgentError("pilot_transcript_missing", "The local pilot transcript is unavailable", retryable=False)
    baseline = automatic_baseline_path(transcript_path)
    if baseline.exists():
        return baseline
    temporary = baseline.with_suffix(".tmp")
    temporary.write_bytes(transcript_path.read_bytes())
    temporary.replace(baseline)
    return baseline


def review_artifact_path(paths: "AgentPaths", call_session_id: str) -> Path:
    # Provider IDs must not become Windows filenames.  The artifact remains
    # local and has no provider URL, cookie, password, or audio bytes.
    safe_name = sha256(call_session_id.encode("utf-8")).hexdigest()
    reviews = paths.reviews
    reviews.mkdir(parents=True, exist_ok=True)
    return reviews / f"{safe_name}.review.json"


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _review_seal(artifact: dict[str, Any], key: bytes) -> str:
    """Authenticate all persisted review fields except the seal itself."""

    unsigned = {name: value for name, value in artifact.items() if name != "seal"}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, "sha256").hexdigest()


def _verify_review_seal(artifact: dict[str, Any]) -> None:
    seal = artifact.get("seal")
    if not isinstance(seal, dict) or seal.get("algorithm") != REVIEW_SEAL_ALGORITHM or seal.get("key_id") != REVIEW_SEAL_KEY_ID:
        raise AgentError("pilot_review_seal_invalid", "The local review seal is invalid", retryable=False)
    value = seal.get("value")
    if not isinstance(value, str) or len(value) != 64:
        raise AgentError("pilot_review_seal_invalid", "The local review seal is invalid", retryable=False)
    expected = _review_seal(artifact, get_review_seal_key())
    if not hmac.compare_digest(value, expected):
        raise AgentError("pilot_review_seal_invalid", "The local review was changed after approval", retryable=False)


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_review_audio(record: "CallRecord") -> None:
    """Bind approval to the downloaded local bytes, never just their DB row."""

    if not record.audio_path or not record.audio_path.is_file() or not record.audio_sha256:
        raise AgentError("pilot_review_source_missing", "The local audio is required for review", retryable=False)
    if not hmac.compare_digest(_file_digest(record.audio_path), record.audio_sha256.casefold()):
        raise AgentError("pilot_review_audio_hash_mismatch", "The local audio no longer matches its downloaded hash", retryable=False)


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def parse_tagged_transcript(value: str) -> list[ReviewTurn]:
    """Parse only the two operator-approved labels without printing text."""
    if not isinstance(value, str):
        raise AgentError("pilot_review_text_invalid", "The reviewed text must be a string", retryable=False)
    turns: list[dict[str, str]] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TAGGED_TURN.match(line)
        if match:
            label_key = match.group("label").strip().casefold()
            role = _ROLE_FOR_LABEL.get(label_key)
            text = _normalized_text(match.group("text"))
            if role is None:
                raise AgentError(
                    "pilot_review_tag_invalid",
                    "Use only [я] or [Клиент] at the beginning of every turn",
                    retryable=False,
                )
            if not text:
                raise AgentError("pilot_review_text_empty", "Each reviewed turn needs text", retryable=False)
            turns.append({"role": role, "label": _DISPLAY_LABEL_FOR_ROLE[role], "text": text})
            continue
        if not turns:
            raise AgentError(
                "pilot_review_tag_missing",
                "Every reviewed transcript must start with [я] or [Клиент]",
                retryable=False,
            )
        turns[-1]["text"] = _normalized_text(f"{turns[-1]['text']} {line}")
    if not turns:
        raise AgentError("pilot_review_text_empty", "The reviewed transcript has no tagged turns", retryable=False)
    return [ReviewTurn(**turn) for turn in turns]


def _milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AgentError("pilot_review_timing_invalid", f"{field} must be an integer number of milliseconds", retryable=False)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise AgentError("pilot_review_timing_invalid", f"{field} must be an integer number of milliseconds", retryable=False)


def build_reviewed_transcript(
    tagged_text: str,
    timings: Any | None,
    *,
    audio_duration_sec: int,
    asr_model: str,
    language: str,
) -> Transcript:
    """Construct a reviewed transcript without inventing missing timecodes."""
    if not isinstance(audio_duration_sec, int) or audio_duration_sec <= 0:
        raise AgentError("pilot_review_audio_duration_invalid", "Local audio duration is unavailable", retryable=False)
    turns = parse_tagged_transcript(tagged_text)
    canonical_text = "\n".join(f"[{turn.label}]: {turn.text}" for turn in turns)
    if timings is None:
        # Text-only review deliberately has no timestamp estimates.  The CRM
        # and GigaChat evidence contract preserves the exact segment ordinal
        # and quote, while presenting the absent time pair as such.
        return Transcript(
            canonical_text,
            [
                TranscriptSegment(
                    ordinal=ordinal,
                    started_ms=None,
                    ended_ms=None,
                    role=turn.role,
                    text=turn.text,
                    speaker_label="operator_review_text_only",
                )
                for ordinal, turn in enumerate(turns)
            ],
            asr_model,
            language,
        )
    if not isinstance(timings, list) or len(timings) != len(turns):
        raise AgentError("pilot_review_timing_count", "Every reviewed turn needs start and end timing", retryable=False)

    duration_ms = audio_duration_sec * 1000
    previous_end = 0
    segments: list[TranscriptSegment] = []
    for ordinal, (turn, timing) in enumerate(zip(turns, timings)):
        if not isinstance(timing, dict):
            raise AgentError("pilot_review_timing_invalid", "Each timing must be an object", retryable=False)
        started_ms = _milliseconds(timing.get("started_ms"), "started_ms")
        ended_ms = _milliseconds(timing.get("ended_ms"), "ended_ms")
        if started_ms < 0 or ended_ms <= started_ms:
            raise AgentError("pilot_review_timing_invalid", "A turn must end after its start", retryable=False)
        if started_ms < previous_end:
            raise AgentError("pilot_review_timing_overlap", "Reviewed turns must not overlap or be out of order", retryable=False)
        if ended_ms > duration_ms:
            raise AgentError("pilot_review_timing_out_of_bounds", "Reviewed turn timing exceeds the local audio duration", retryable=False)
        segments.append(
            TranscriptSegment(
                ordinal=ordinal,
                started_ms=started_ms,
                ended_ms=ended_ms,
                role=turn.role,
                text=turn.text,
                speaker_label="operator_review",
            )
        )
        previous_end = ended_ms

    return Transcript(canonical_text, segments, asr_model, language)


def _edit_distance(left: list[str], right: list[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_value != right_value),
            ))
        previous = current
    return previous[-1]


def _tokenize(value: str) -> list[str]:
    return re.findall(r"\w+", value.casefold(), flags=re.UNICODE)


def _rate(hypothesis: list[str], reference: list[str]) -> float | None:
    if not reference:
        return None
    return round(_edit_distance(hypothesis, reference) / len(reference), 6)


def quality_metrics(automatic: Transcript, reviewed: Transcript, *, automatic_roles_available: bool) -> dict[str, Any]:
    """Calculate local-only text/role quality metrics; never log utterances."""
    automatic_text = " ".join(segment.text for segment in automatic.segments)
    reviewed_text = " ".join(segment.text for segment in reviewed.segments)
    automatic_words = _tokenize(automatic_text)
    reviewed_words = _tokenize(reviewed_text)
    automatic_characters = list("".join(automatic_words))
    reviewed_characters = list("".join(reviewed_words))

    reviewed_intervals_available = all(
        isinstance(segment.started_ms, int)
        and not isinstance(segment.started_ms, bool)
        and isinstance(segment.ended_ms, int)
        and not isinstance(segment.ended_ms, bool)
        for segment in reviewed.segments
    )
    role_metrics_available = automatic_roles_available and reviewed_intervals_available
    result: dict[str, Any] = {
        "word_error_rate": _rate(automatic_words, reviewed_words),
        "character_error_rate": _rate(automatic_characters, reviewed_characters),
        "reference_word_count": len(reviewed_words),
        "automatic_segment_count": len(automatic.segments),
        "reviewed_turn_count": len(reviewed.segments),
        "automatic_role_metrics_available": role_metrics_available,
    }
    if not role_metrics_available or not automatic.segments:
        result.update({
            "automatic_role_coverage": None,
            "automatic_role_accuracy_when_assigned": None,
            "automatic_role_assigned_segments": 0,
        })
        return result

    known = correct = 0
    for automatic_segment in automatic.segments:
        if automatic_segment.role not in {"manager", "customer"}:
            continue
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (automatic_segment.started_ms, automatic_segment.ended_ms)
        ):
            continue
        known += 1
        overlaps = [
            (max(0, min(automatic_segment.ended_ms, reviewed_segment.ended_ms) - max(automatic_segment.started_ms, reviewed_segment.started_ms)), reviewed_segment)
            for reviewed_segment in reviewed.segments
        ]
        overlap, matched = max(overlaps, key=lambda item: item[0], default=(0, None))
        if overlap and matched and automatic_segment.role == matched.role:
            correct += 1
    result.update({
        "automatic_role_coverage": round(known / len(automatic.segments), 6),
        "automatic_role_accuracy_when_assigned": round(correct / known, 6) if known else None,
        "automatic_role_assigned_segments": known,
    })
    return result


def _load_json(path: Path, error_code: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(error_code, "Local review data is unreadable", retryable=False) from exc
    if not isinstance(raw, dict):
        raise AgentError(error_code, "Local review data has an invalid shape", retryable=False)
    return raw


def create_approved_review(
    paths: "AgentPaths",
    record: "CallRecord",
    *,
    tagged_text: str,
    timings: Any | None,
) -> Path:
    """Persist one immutable approved review artifact on the home PC only."""
    if not record.transcript_path or not record.audio_sha256 or not record.audio_duration_sec:
        raise AgentError("pilot_review_source_missing", "The local ASR source metadata is incomplete", retryable=False)
    _verify_review_audio(record)
    source_path = preserve_automatic_baseline(record.transcript_path)
    automatic_raw = _load_json(source_path, "pilot_review_source_invalid")
    try:
        automatic = Transcript.from_path(source_path)
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("pilot_review_source_invalid", "The local ASR source is invalid", retryable=False) from exc
    reviewed = build_reviewed_transcript(
        tagged_text,
        timings,
        audio_duration_sec=record.audio_duration_sec,
        # CRM must distinguish the approved human transcription from the raw
        # GigaAM output while keeping the exact source model in ``source``.
        asr_model=f"operator-reviewed:{automatic.asr_model}",
        language=automatic.language,
    )
    timing_mode = TIMING_MODE_TEXT_ONLY if timings is None else TIMING_MODE_MANUAL
    review_payload = {
        "tagged_text": reviewed.text,
        "segments": [asdict(segment) for segment in reviewed.segments],
        "timing_mode": timing_mode,
    }
    artifact = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "state": "approved",
        "call_session_id": record.call_session_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "automatic_transcript_sha256": _file_digest(source_path),
            "audio_sha256": record.audio_sha256,
            "audio_duration_sec": record.audio_duration_sec,
            "asr_model": automatic.asr_model,
            "language": automatic.language,
        },
        "review": review_payload,
        "review_sha256": _json_digest(review_payload),
        "metrics": quality_metrics(
            automatic,
            reviewed,
            automatic_roles_available="manual_role_assignment" not in automatic_raw,
        ),
    }
    artifact["seal"] = {
        "algorithm": REVIEW_SEAL_ALGORITHM,
        "key_id": REVIEW_SEAL_KEY_ID,
        "value": _review_seal(artifact, get_or_create_review_seal_key()),
    }
    destination = review_artifact_path(paths, record.call_session_id)
    if destination.exists():
        raise AgentError("pilot_review_already_approved", "This pilot already has an approved local review", retryable=False)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_approved_review(paths: "AgentPaths", record: "CallRecord") -> Transcript:
    """Return only a verified approved review for text-only CRM delivery."""
    if not record.transcript_path or not record.audio_sha256 or not record.audio_duration_sec:
        raise AgentError("pilot_review_source_missing", "The local ASR source metadata is incomplete", retryable=False)
    destination = review_artifact_path(paths, record.call_session_id)
    if not destination.is_file():
        raise AgentError("pilot_review_missing", "Approve a local review before CRM delivery", retryable=False)
    raw = _load_json(destination, "pilot_review_invalid")
    if raw.get("schema_version") != REVIEW_SCHEMA_VERSION or raw.get("state") != "approved":
        raise AgentError("pilot_review_invalid", "The local review is not approved", retryable=False)
    if raw.get("call_session_id") != record.call_session_id:
        raise AgentError("pilot_review_mismatch", "The review belongs to a different call", retryable=False)
    _verify_review_seal(raw)
    source = raw.get("source")
    review = raw.get("review")
    if not isinstance(source, dict) or not isinstance(review, dict):
        raise AgentError("pilot_review_invalid", "The local review has an invalid shape", retryable=False)
    baseline = automatic_baseline_path(record.transcript_path)
    if not baseline.is_file() or source.get("automatic_transcript_sha256") != _file_digest(baseline):
        raise AgentError("pilot_review_source_changed", "The local ASR source no longer matches the approved review", retryable=False)
    if source.get("audio_sha256") != record.audio_sha256 or source.get("audio_duration_sec") != record.audio_duration_sec:
        raise AgentError("pilot_review_audio_mismatch", "The approved review does not match local audio metadata", retryable=False)
    if raw.get("review_sha256") != _json_digest(review):
        raise AgentError("pilot_review_changed", "The approved review was changed after approval", retryable=False)
    timing_mode = review.get("timing_mode", TIMING_MODE_MANUAL)
    if timing_mode == TIMING_MODE_TEXT_ONLY:
        timings: Any | None = None
    elif timing_mode == TIMING_MODE_MANUAL:
        try:
            timings = [
                {"started_ms": segment["started_ms"], "ended_ms": segment["ended_ms"]}
                for segment in review["segments"]
            ]
        except (KeyError, TypeError) as exc:
            raise AgentError("pilot_review_invalid", "The approved review has invalid segments", retryable=False) from exc
    else:
        raise AgentError("pilot_review_invalid", "The approved review has an unknown timing mode", retryable=False)
    try:
        transcript = build_reviewed_transcript(
            str(review["tagged_text"]),
            timings,
            audio_duration_sec=record.audio_duration_sec,
            asr_model=f"operator-reviewed:{source['asr_model']}",
            language=str(source["language"]),
        )
    except (KeyError, TypeError) as exc:
        raise AgentError("pilot_review_invalid", "The approved review has invalid segments", retryable=False) from exc
    expected_segments = review.get("segments")
    if [asdict(segment) for segment in transcript.segments] != expected_segments:
        raise AgentError("pilot_review_changed", "The approved review segments do not match its tagged text", retryable=False)
    return transcript


def _default_review_text(automatic: Transcript) -> str:
    """Expose one role-neutral draft for the keyboard-first editor.

    The reviewer decides who speaks first and presses Enter only at actual
    speaker changes. Showing the diarizer's tentative labels here would make
    a human correction look like an error-prone tagging exercise.
    """

    return _normalized_text(" ".join(segment.text for segment in automatic.segments))


@dataclass
class _ReviewContext:
    paths: "AgentPaths"
    record: "CallRecord"
    draft_text: str
    approved_path: Path | None = None


class _ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: _ReviewContext):
        self.context = context
        super().__init__(address, _ReviewRequestHandler)


class _ReviewRequestHandler(BaseHTTPRequestHandler):
    server: _ReviewHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Default request logging can include URL/query data; keep the review
        # page entirely local and avoid emitting any transcript-related data.
        return

    def _local_only(self) -> bool:
        return self.client_address[0] == "127.0.0.1"

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'none'; media-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _forbidden_if_not_local(self) -> bool:
        if self._local_only():
            return False
        self._send_json(403, {"error": "local_only"})
        return True

    def _forbidden_if_cross_origin(self) -> bool:
        """Reject a browser request that did not originate at this loopback UI."""
        origin = self.headers.get("Origin")
        if not origin:
            return False
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if origin == expected:
            return False
        self._send_json(403, {"error": "local_origin_required"})
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self._forbidden_if_not_local():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._serve_editor()
        elif path == "/audio":
            self._serve_audio()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._forbidden_if_not_local() or self._forbidden_if_cross_origin():
            return
        path = urlparse(self.path).path
        if path == "/cancel":
            self._send_json(200, {"cancelled": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path != "/approve":
            self._send_json(404, {"error": "not_found"})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            self._send_json(400, {"error": "pilot_review_request_invalid"})
            return
        if length <= 0 or length > MAX_REVIEW_REQUEST_BYTES:
            self._send_json(413, {"error": "pilot_review_request_too_large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            artifact = create_approved_review(
                self.server.context.paths,
                self.server.context.record,
                tagged_text=body.get("tagged_text"),
                timings=body.get("timings"),
            )
        except AgentError as exc:
            self._send_json(400, {"error": exc.code})
            return
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": "pilot_review_request_invalid"})
            return
        self.server.context.approved_path = artifact
        self._send_json(200, {"approved": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _serve_editor(self) -> None:
        context = self.server.context
        page = _editor_html(context.draft_text)
        body = page.encode("utf-8")
        self._headers(200, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _serve_audio(self) -> None:
        audio = self.server.context.record.audio_path
        if not audio or not audio.is_file():
            self._send_json(404, {"error": "pilot_audio_missing"})
            return
        size = audio.stat().st_size
        content_type = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
        if audio.suffix.casefold() == ".m4a":
            content_type = "audio/mp4"
        start = 0
        end = size - 1
        status = 200
        requested_range = self.headers.get("Range")
        if requested_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range.strip())
            if not match or not size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = int(right) if right else end
            else:
                suffix = int(right)
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        with audio.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining and (block := handle.read(min(1024 * 1024, remaining))):
                self.wfile.write(block)
        remaining -= len(block)


def _editor_html(draft_text: str) -> str:
    """Render a text-only, keyboard-first local review editor.

    The operator controls only text and speaker changes.  Audio remains a
    local optional reference, while missing per-turn timecodes are explicitly
    represented as ``null`` all the way to the CRM rather than estimated.
    """

    initial_text_json = (
        json.dumps(draft_text, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    page = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MO54 Calls — локальная проверка</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#17212b;background:#f4f7fa}
*{box-sizing:border-box}body{margin:0;line-height:1.45}.page{max-width:960px;margin:0 auto;padding:clamp(1rem,4vw,3rem) 1rem 4rem}h1{margin:0;font-size:clamp(1.45rem,3vw,2rem);letter-spacing:-.02em}.lead{max-width:72ch;color:#425466;margin:.65rem 0 1.25rem}.card{background:#fff;border:1px solid #d9e1e8;border-radius:14px;box-shadow:0 8px 25px rgba(28,45,64,.06);padding:clamp(1rem,3vw,1.5rem);margin:1rem 0}.guide{display:grid;grid-template-columns:auto 1fr;gap:.55rem .75rem;margin:0;padding:0;list-style:none}.guide b{display:inline-grid;place-items:center;width:1.65rem;height:1.65rem;border-radius:999px;background:#e7f0fa;color:#155f98}.controls{display:flex;align-items:center;gap:.75rem 1rem;justify-content:space-between;flex-wrap:wrap;margin:.9rem 0}.first-speaker{font-weight:650}.first-speaker select{margin-left:.45rem;font:inherit;padding:.35rem .5rem;border:1px solid #9aacbd;border-radius:7px;background:#fff;color:#17212b}audio{display:block;width:100%;margin:1rem 0}.hint{font-size:.94rem;color:#526576;margin:.65rem 0 0}.turns{margin-top:1rem;border-top:1px solid #d9e1e8}.turn{display:grid;grid-template-columns:7.4rem minmax(0,1fr);gap:.75rem;padding:1rem 0;border-bottom:1px solid #d9e1e8;align-items:start}.role{min-height:2.5rem;width:100%;border:1px solid #a8c2d9;border-radius:999px;background:#eef6fd;color:#154f7d;font-weight:700;cursor:pointer}.role.customer{background:#f7f0e4;border-color:#d8ba82;color:#735215}.turn textarea{resize:vertical;width:100%;min-height:4.75rem;border:1px solid #aebdca;border-radius:9px;padding:.7rem .75rem;color:#17212b;background:#fff;font:inherit;line-height:1.5}.turn textarea:focus,.role:focus-visible,.first-speaker select:focus-visible,button:focus-visible{outline:3px solid #1769aa;outline-offset:2px}.turn textarea:focus{border-color:#1769aa}.actions{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-top:1rem}.primary,.secondary{min-height:2.7rem;border-radius:8px;padding:.55rem .9rem;font:inherit;font-weight:650;cursor:pointer}.primary{border:1px solid #0d5c96;background:#1269a8;color:#fff}.secondary{border:1px solid #b2bfca;background:#fff;color:#263847}.primary:disabled{cursor:wait;opacity:.65}.message{min-height:1.45rem;margin:.8rem 0 0;font-weight:650}.message.error{color:#9b1c1c}.message.success{color:#176b3a}.privacy{font-size:.9rem;color:#526576;margin:.95rem 0}@media(max-width:620px){.turn{grid-template-columns:1fr}.role{width:auto;padding-inline:1rem}.guide{grid-template-columns:auto 1fr}}
</style></head><body><main class="page">
<h1>Проверка диалога</h1>
<p class="lead">Работайте только с текстом. Когда начинается реплика другого человека, поставьте курсор перед её первой фразой и нажмите <kbd>Enter</kbd>.</p>
<section class="card" aria-label="Как разделять разговор"><ol class="guide"><li><b>1</b></li><li>Выберите, кто сказал первую фразу.</li><li><b>2</b></li><li>Исправляйте текст как обычно. Перед первой фразой следующего человека нажмите <kbd>Enter</kbd>.</li><li><b>3</b></li><li>Роль новой строки переключится сама. Если ошиблись, нажмите <kbd>Backspace</kbd> в начале новой строки.</li></ol></section>
<section class="card" aria-label="Редактор диалога"><div class="controls"><label class="first-speaker" for="first-speaker">Первым говорит<select id="first-speaker" data-testid="first-speaker"><option value="manager">Я</option><option value="customer">Клиент</option></select></label></div><p class="hint">Аудио можно включить только для проверки спорного места — ставить его на границы реплик не нужно. <kbd>Shift+Enter</kbd> оставляет перенос внутри реплики. Роль в плашке можно переключить кликом.</p><audio id="audio" controls preload="metadata" src="/audio"></audio><div id="turns" class="turns" aria-label="Реплики разговора" data-testid="turns"></div><div class="actions"><button type="button" id="approve" class="primary" data-testid="approve">Утвердить локальную версию</button><button type="button" id="cancel" class="secondary">Отменить без сохранения</button></div><p id="message" class="message" role="status" aria-live="polite"></p><p class="privacy">Временные отметки реплик не создаются. Аудио и текст остаются на этом ПК до отдельной команды доставки в CRM.</p></section>
</main><script>
const turnsElement=document.getElementById('turns');const firstSpeaker=document.getElementById('first-speaker');const message=document.getElementById('message');const approve=document.getElementById('approve');const initialText=__INITIAL_TEXT_JSON__;const labels={manager:'Я',customer:'Клиент'};const otherRole=role=>role==='manager'?'customer':'manager';let turns=[{role:firstSpeaker.value,text:initialText}];
function cleanText(value){return String(value??'').replace(/\s+/g,' ').trim();}function setMessage(value,kind='error'){message.className=`message ${kind}`;message.textContent=value;}function clearMessage(){message.className='message';message.textContent='';}
function focusTurn(index,caret){const field=turnsElement.querySelector(`textarea[data-turn-index="${index}"]`);if(!field)return;field.focus();const point=Math.min(Math.max(0,caret),field.value.length);field.setSelectionRange(point,point);}
function render(focus){turnsElement.replaceChildren();turns.forEach((turn,index)=>{const row=document.createElement('div');row.className='turn';const role=document.createElement('button');role.type='button';role.className=`role ${turn.role}`;role.dataset.testid='turn-role';role.textContent=labels[turn.role];role.setAttribute('aria-label',`Сменить роль реплики ${index+1}, сейчас: ${labels[turn.role]}`);role.addEventListener('click',()=>{turn.role=otherRole(turn.role);render({index,caret:0});setMessage(`Роль реплики ${index+1}: ${labels[turn.role]}.`,'success');});const field=document.createElement('textarea');field.dataset.turnIndex=String(index);field.value=turn.text;field.placeholder='Текст реплики';field.setAttribute('aria-label',`Реплика ${index+1}, ${labels[turn.role]}`);field.addEventListener('input',()=>{turn.text=field.value;clearMessage();});field.addEventListener('keydown',event=>handleEditorKey(event,index,field));row.append(role,field);turnsElement.append(row);});if(focus)requestAnimationFrame(()=>focusTurn(focus.index,focus.caret));}
function splitTurn(index,field){if(field.selectionStart!==field.selectionEnd){setMessage('Сначала снимите выделение, затем поставьте курсор перед первой фразой другого человека.');return;}const caret=field.selectionStart;const before=field.value.slice(0,caret).replace(/\s+$/,'');const after=field.value.slice(caret).replace(/^\s+/,'');if(!cleanText(before)||!cleanText(after)){setMessage('Поставьте курсор между двумя фразами: текст до и после него должен остаться в разных репликах.');return;}turns[index].text=before;turns.splice(index+1,0,{role:otherRole(turns[index].role),text:after});clearMessage();render({index:index+1,caret:0});}
function mergeTurnWithPrevious(index){if(index===0)return;const previous=turns[index-1];const current=turns[index];const caret=previous.text.length+(previous.text&&current.text?' ':'');previous.text=[previous.text.trimEnd(),current.text.trimStart()].filter(Boolean).join(' ');turns.splice(index,1);clearMessage();render({index:index-1,caret});}
function handleEditorKey(event,index,field){if(event.isComposing)return;if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();splitTurn(index,field);return;}if(event.key==='Backspace'&&index>0&&field.selectionStart===0&&field.selectionEnd===0){event.preventDefault();mergeTurnWithPrevious(index);}}
function resetRolesFromFirstSpeaker(){let role=firstSpeaker.value;turns=turns.map(turn=>{const next={...turn,role};role=otherRole(role);return next;});clearMessage();render();}
function reviewedPayload(){if(!turns.length)throw new Error('Добавьте хотя бы одну реплику.');const reviewed=turns.map((turn,index)=>{const text=cleanText(turn.text);if(!text)throw new Error(`Заполните текст реплики ${index+1}.`);return {...turn,text};});return {tagged_text:reviewed.map(turn=>`[${labels[turn.role]}]: ${turn.text}`).join('\n'),timings:null};}
firstSpeaker.addEventListener('change',resetRolesFromFirstSpeaker);document.getElementById('approve').addEventListener('click',async()=>{let payload;try{payload=reviewedPayload();}catch(error){setMessage(error.message);return;}approve.disabled=true;clearMessage();try{const response=await fetch('/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json();if(!response.ok){setMessage('Не удалось утвердить версию. Проверьте текст и роли реплик.');approve.disabled=false;return;}setMessage('Локальная версия утверждена. Окно можно закрыть.','success');}catch(_error){setMessage('Не удалось сохранить локальную версию. Проверьте соединение с локальным редактором.');approve.disabled=false;}});document.getElementById('cancel').addEventListener('click',async()=>{await fetch('/cancel',{method:'POST'});setMessage('Проверка отменена без сохранения.','success');});render();
</script></body></html>"""
    return page.replace("__INITIAL_TEXT_JSON__", initial_text_json)


def review_pilot(
    paths: "AgentPaths",
    record: "CallRecord",
    *,
    on_started: Callable[[str], None] | None = None,
) -> Path:
    """Open a short-lived loopback-only review UI and wait for approval."""
    if not record.audio_path or not record.audio_path.is_file() or not record.audio_duration_sec or not record.transcript_path:
        raise AgentError("pilot_review_source_missing", "The local audio and transcript are required for review", retryable=False)
    _verify_review_audio(record)
    if review_artifact_path(paths, record.call_session_id).exists():
        raise AgentError("pilot_review_already_approved", "This pilot already has an approved local review", retryable=False)
    try:
        # Snapshot first, before any manual role assignment can change the
        # working transcript.  The editor itself always reads this baseline.
        automatic = Transcript.from_path(preserve_automatic_baseline(record.transcript_path))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise AgentError("pilot_review_source_invalid", "The local ASR source is invalid", retryable=False) from exc
    context = _ReviewContext(paths, record, _default_review_text(automatic))
    server = _ReviewHTTPServer(("127.0.0.1", 0), context)
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        if on_started:
            on_started(url)
        webbrowser.open(url, new=1)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    if not context.approved_path:
        raise AgentError("pilot_review_not_approved", "The local review was closed before approval", retryable=False)
    return context.approved_path
