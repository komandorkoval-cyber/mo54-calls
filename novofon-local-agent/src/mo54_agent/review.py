from __future__ import annotations

import html
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
    timings: Any,
    *,
    audio_duration_sec: int,
    asr_model: str,
    language: str,
) -> Transcript:
    """Validate manual timing data and construct the CRM-safe reviewed text."""
    if not isinstance(audio_duration_sec, int) or audio_duration_sec <= 0:
        raise AgentError("pilot_review_audio_duration_invalid", "Local audio duration is unavailable", retryable=False)
    turns = parse_tagged_transcript(tagged_text)
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

    canonical_text = "\n".join(f"[{turn.label}]: {turn.text}" for turn in turns)
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

    result: dict[str, Any] = {
        "word_error_rate": _rate(automatic_words, reviewed_words),
        "character_error_rate": _rate(automatic_characters, reviewed_characters),
        "reference_word_count": len(reviewed_words),
        "automatic_segment_count": len(automatic.segments),
        "reviewed_turn_count": len(reviewed.segments),
        "automatic_role_metrics_available": automatic_roles_available,
    }
    if not automatic_roles_available or not automatic.segments:
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
    timings: Any,
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
    review_payload = {
        "tagged_text": reviewed.text,
        "segments": [asdict(segment) for segment in reviewed.segments],
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
    try:
        transcript = build_reviewed_transcript(
            str(review["tagged_text"]),
            [
                {"started_ms": segment["started_ms"], "ended_ms": segment["ended_ms"]}
                for segment in review["segments"]
            ],
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
    """Expose a local draft only; unknown roles are intentionally not guessed."""
    rows = []
    for segment in automatic.segments:
        label = _DISPLAY_LABEL_FOR_ROLE.get(segment.role, "укажите роль")
        rows.append(f"[{label}]: {segment.text}")
    return "\n".join(rows)


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
        duration_ms = context.record.audio_duration_sec * 1000 if context.record.audio_duration_sec else 0
        page = _editor_html(context.draft_text, duration_ms)
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


def _editor_html(draft_text: str, duration_ms: int) -> str:
    escaped_draft = html.escape(draft_text)
    return rf"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MO54 Calls — локальная проверка</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#17212b;line-height:1.45}} textarea{{box-sizing:border-box;width:100%;min-height:20rem;padding:.75rem;font:14px ui-monospace,monospace;line-height:1.5}} input{{width:8rem}} button{{min-height:2.5rem;padding:.55rem .9rem;margin:.75rem .5rem .75rem 0;cursor:pointer}} button:focus-visible,input:focus-visible,textarea:focus-visible{{outline:3px solid #1769aa;outline-offset:2px}} button:disabled{{cursor:wait;opacity:.65}} .turn{{display:grid;grid-template-columns:7rem 1fr 13rem 13rem;gap:.75rem;margin:.6rem 0;padding:.6rem;border-block-end:1px solid #d9e1e8;align-items:start}} .turn span{{overflow-wrap:anywhere}} .time{{display:flex;gap:.25rem;align-items:center}} .time button{{min-height:2rem;margin:0;padding:.3rem .45rem;font-size:.85rem}} .error{{color:#9b1c1c;font-weight:600}} .success{{color:#176b3a;font-weight:600}} .actions{{display:flex;gap:.5rem;flex-wrap:wrap}} small{{color:#57606a}} @media(max-width:720px){{.turn{{grid-template-columns:1fr}} .time{{max-width:22rem}}}}
</style></head><body>
<h1>Локальная проверка транскрипта</h1>
<p id="instructions">Страница доступна только на этом ПК. Сначала проверьте текст с тегами ролей, затем для каждой реплики отметьте начало и конец по плееру. Аудио и текст не передаются до отдельной команды доставки.</p>
<audio controls preload="metadata" src="/audio"></audio>
<p><small>Длительность аудио: {duration_ms} мс. Используйте только теги <code>[я]</code> и <code>[Клиент]</code>.</small></p>
<label for="text"><strong>Утверждённый текст</strong></label>
<textarea id="text" spellcheck="false" aria-describedby="instructions">{escaped_draft}</textarea>
<div class="actions"><button type="button" id="prepare">Разметить реплики</button><button type="button" id="cancel">Отменить без сохранения</button></div>
<div id="turns" aria-label="Временные границы реплик"></div><p id="message" class="error" aria-live="polite"></p>
<button type="button" id="approve">Утвердить локальную версию</button>
<script>
const text=document.getElementById('text'), turns=document.getElementById('turns'), message=document.getElementById('message'), audio=document.querySelector('audio');
const errorMessages={{pilot_review_text_invalid:'Текст должен состоять из реплик.',pilot_review_tag_invalid:'Используйте только теги [я] и [Клиент].',pilot_review_tag_missing:'Первая реплика должна начинаться с тега роли.',pilot_review_text_empty:'Заполните текст каждой реплики.',pilot_review_timing_count:'Укажите начало и конец для каждой реплики.',pilot_review_timing_invalid:'Время вводится целым числом миллисекунд; конец должен быть позже начала.',pilot_review_timing_overlap:'Реплики не должны пересекаться и должны идти по порядку.',pilot_review_timing_out_of_bounds:'Время реплики выходит за пределы длительности аудио.',pilot_review_already_approved:'Эта версия уже была утверждена.',local_origin_required:'Окно проверки нужно открыть только с этого ПК.'}};
function showError(code){{message.className='error';message.textContent=errorMessages[code]||'Не удалось утвердить версию. Проверьте текст и времена.';}}
function parse(){{
  const entries=[]; let current=null;
  for(const raw of text.value.split(/\r?\n/)){{ const line=raw.trim(); if(!line) continue;
    const hit=line.match(/^\s*\[([^\]]+)\]\s*:\s*(.*?)\s*$/); if(hit){{
      const label=hit[1].trim().toLocaleLowerCase('ru-RU'); if(label!=='я' && label!=='клиент') throw Error('Каждая реплика должна начинаться с [я] или [Клиент].');
      if(!hit[2].trim()) throw Error('Пустая реплика не допускается.'); current={{label:label==='я'?'я':'Клиент',text:hit[2].trim()}}; entries.push(current);
    }} else {{ if(!current) throw Error('Первая реплика должна начинаться с тега роли.'); current.text += ' '+line; }}
  }} if(!entries.length) throw Error('Введите хотя бы одну реплику.'); return entries;
}}
function render(){{ message.textContent=''; let entries; try{{entries=parse();}}catch(error){{message.textContent=error.message; return;}}
  turns.replaceChildren(); entries.forEach((entry,index)=>{{const row=document.createElement('div');row.className='turn';row.dataset.index=index;
    const role=document.createElement('strong');role.textContent='['+entry.label+']'; const utterance=document.createElement('span');utterance.textContent=entry.text;
    const start=document.createElement('input');start.name='started_ms';start.inputMode='numeric';start.placeholder='начало, мс';start.setAttribute('aria-label','Начало реплики '+(index+1)+' в миллисекундах'); const setStart=document.createElement('button');setStart.type='button';setStart.textContent='Взять с плеера';setStart.setAttribute('aria-label','Взять начало реплики '+(index+1)+' с текущей позиции плеера');setStart.addEventListener('click',()=>start.value=Math.round(audio.currentTime*1000)); const startWrap=document.createElement('span');startWrap.className='time';startWrap.append(start,setStart);
    const end=document.createElement('input');end.name='ended_ms';end.inputMode='numeric';end.placeholder='конец, мс';end.setAttribute('aria-label','Конец реплики '+(index+1)+' в миллисекундах'); const setEnd=document.createElement('button');setEnd.type='button';setEnd.textContent='Взять с плеера';setEnd.setAttribute('aria-label','Взять конец реплики '+(index+1)+' с текущей позиции плеера');setEnd.addEventListener('click',()=>end.value=Math.round(audio.currentTime*1000)); const endWrap=document.createElement('span');endWrap.className='time';endWrap.append(end,setEnd);
    row.append(role,utterance,startWrap,endWrap);turns.append(row); }});
}}
document.getElementById('prepare').addEventListener('click',render);
document.getElementById('cancel').addEventListener('click',async()=>{{await fetch('/cancel',{{method:'POST'}});message.className='';message.textContent='Проверка отменена без сохранения.';}});
document.getElementById('approve').addEventListener('click',async()=>{{
  const approve=document.getElementById('approve');message.textContent=''; if(!turns.children.length) render(); if(!turns.children.length) return; approve.disabled=true;
  const timings=[...turns.children].map(row=>({{started_ms:row.querySelector('[name=started_ms]').value,ended_ms:row.querySelector('[name=ended_ms]').value}}));
  try{{const response=await fetch('/approve',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tagged_text:text.value,timings}})}});const result=await response.json();if(!response.ok){{showError(result.error);approve.disabled=false;return;}} message.className='success';message.textContent='Локальная версия утверждена. Окно можно закрыть.';}}catch(_error){{showError('network');approve.disabled=false;}}
}});
</script></body></html>"""


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
