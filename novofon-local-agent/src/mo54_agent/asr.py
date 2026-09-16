from __future__ import annotations

import json
import math
import shutil
import subprocess
from urllib.request import Request, urlopen
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .browser import media_duration
from .config import AgentConfig, AgentPaths
from .errors import AgentError
from .media import find_media_tool
from .security import load_manager_embedding, save_manager_embedding


ASR_MODEL = "gigaam-v3-e2e-rnnt"
GIGAAM_MODEL_NAME = "v3_e2e_rnnt"
LANGUAGE = "ru"
CHUNK_SECONDS = 24


@dataclass(frozen=True)
class TranscriptSegment:
    ordinal: int
    # A human may approve the text and roles without manually placing audio
    # boundaries.  In that text-only review mode both values are deliberately
    # absent rather than guessed from word count or ASR chunks.
    started_ms: int | None
    ended_ms: int | None
    role: str
    text: str
    speaker_label: str | None = None


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[TranscriptSegment]
    asr_model: str = ASR_MODEL
    language: str = LANGUAGE

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "asr_model": self.asr_model, "language": self.language, "segments": [asdict(item) for item in self.segments]}

    @classmethod
    def from_path(cls, path: Path) -> "Transcript":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(raw["text"], [TranscriptSegment(**segment) for segment in raw["segments"]], raw["asr_model"], raw["language"])


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left or not right:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    scale = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / scale if scale else -1.0


def role_map_for_speakers(
    speakers: list[str], scores: dict[str, float], threshold: float
) -> dict[str, str]:
    """Only name roles when the manager reference gives one unambiguous match."""
    result = {speaker: "unknown" for speaker in speakers}
    if not scores:
        return result
    manager = max(scores, key=scores.get)
    if scores[manager] < threshold:
        return result
    result[manager] = "manager"
    others = [speaker for speaker in speakers if speaker != manager]
    if len(others) == 1:
        result[others[0]] = "customer"
    return result


class LocalASR:
    """GigaAM plus entirely local pyannote diarization and role matching."""

    def __init__(self, paths: AgentPaths, config: AgentConfig):
        self.paths = paths
        self.config = config
        self._gigaam = None
        self._diarization = None
        self._embedding = None

    @property
    def diarization_model_path(self) -> Path:
        return self.paths.models / "pyannote-speaker-diarization-community-1"

    def verify_ready(self) -> list[str]:
        problems: list[str] = []
        if find_media_tool("ffmpeg") is None or find_media_tool("ffprobe") is None:
            problems.append("ffmpeg_missing")
        if not self.diarization_model_path.is_dir():
            problems.append("pyannote_model_missing")
        try:
            import gigaam  # noqa: F401
        except ImportError:
            problems.append("gigaam_runtime_missing")
        return problems

    def prepare_models(self, hf_token: str) -> None:
        """Download gated diarization weights once. The supplied token is never persisted."""
        if not hf_token:
            raise AgentError("huggingface_token_missing", "A one-time Hugging Face token is required to fetch the accepted models", retryable=False)
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id="pyannote/speaker-diarization-community-1",
                local_dir=str(self.diarization_model_path),
                token=hf_token,
            )
        except Exception as exc:
            raise AgentError("pyannote_model_download_failed", "pyannote model download failed", retryable=True) from exc
        try:
            # GigaAM weights are public, but must still remain in the agent-owned
            # directory rather than the user's global Python cache.
            self._load_gigaam()
        except Exception as exc:
            raise AgentError("gigaam_model_download_failed", "GigaAM model download failed", retryable=True) from exc

    def _load_gigaam(self):
        if self._gigaam is None:
            try:
                from gigaam import _MODEL_HASHES, _URL_DIR, load_model
                model_root = self.paths.models / "gigaam"
                checkpoint = model_root / f"{GIGAAM_MODEL_NAME}.ckpt"
                self._download_resumable(checkpoint, f"{_URL_DIR}/{GIGAAM_MODEL_NAME}.ckpt", _MODEL_HASHES[GIGAAM_MODEL_NAME])
                self._gigaam = load_model(GIGAAM_MODEL_NAME, download_root=str(model_root))
            except AgentError:
                raise
            except Exception as exc:
                raise AgentError("gigaam_model_unavailable", "GigaAM local model is unavailable", retryable=True) from exc
        return self._gigaam

    @staticmethod
    def _md5(path: Path) -> str:
        digest = __import__("hashlib").md5()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _download_resumable(self, destination: Path, url: str, expected_md5: str) -> None:
        """Download public GigaAM weights with resume and a mandatory checksum."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and self._md5(destination) == expected_md5:
            return
        partial = destination.with_suffix(destination.suffix + ".part")
        if destination.is_file() and not partial.exists():
            destination.replace(partial)
        offset = partial.stat().st_size if partial.exists() else 0
        curl = shutil.which("curl.exe")
        if curl:
            command = [curl, "--fail", "--location", "--retry", "5", "--retry-all-errors", "--output", str(partial)]
            if offset:
                command += ["--continue-at", "-"]
            command.append(url)
            try:
                subprocess.run(command, check=True, timeout=3_600)
            except (OSError, subprocess.SubprocessError) as exc:
                raise AgentError("gigaam_download_interrupted", "GigaAM download can be resumed", retryable=True) from exc
            if self._md5(partial) != expected_md5:
                raise AgentError("gigaam_checksum_or_download_incomplete", "GigaAM download is incomplete; it can be resumed", retryable=True)
            partial.replace(destination)
            return
        request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        with urlopen(request, timeout=60) as response:
            if offset and response.status != 206:
                partial.unlink(missing_ok=True)
                return self._download_resumable(destination, url, expected_md5)
            mode = "ab" if offset else "wb"
            with partial.open(mode) as handle:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(block)
        if self._md5(partial) != expected_md5:
            raise AgentError("gigaam_checksum_or_download_incomplete", "GigaAM download is incomplete; it can be resumed", retryable=True)
        partial.replace(destination)

    def _load_diarization(self):
        if self._diarization is None:
            try:
                import torch
                from pyannote.audio import Pipeline
                pipeline = Pipeline.from_pretrained(str(self.diarization_model_path))
                if torch.cuda.is_available():
                    pipeline.to(torch.device("cuda"))
                self._diarization = pipeline
            except Exception as exc:
                raise AgentError("diarization_model_unavailable", "Local diarization model is unavailable", retryable=True) from exc
        return self._diarization

    def _load_embedding(self):
        if self._embedding is None:
            try:
                from pyannote.audio import Inference, Model
                model = Model.from_pretrained(str(self.diarization_model_path / "embedding"))
                self._embedding = Inference(model, window="whole")
            except Exception as exc:
                raise AgentError("voice_embedding_unavailable", "Local manager voice embedding model is unavailable", retryable=True) from exc
        return self._embedding

    def _normalize(self, source: Path, target: Path, *, start: int | None = None, duration: int | None = None) -> None:
        ffmpeg = find_media_tool("ffmpeg")
        if not ffmpeg:
            raise AgentError("ffmpeg_missing", "ffmpeg is required for local audio normalization", retryable=False)
        command = [ffmpeg, "-nostdin", "-y"]
        if start is not None:
            command += ["-ss", str(start)]
        command += ["-i", str(source)]
        if duration is not None:
            command += ["-t", str(duration)]
        command += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)]
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError("audio_normalization_failed", "ffmpeg could not normalize local audio", retryable=True) from exc

    def enroll_manager(self, reference_audio: Path) -> None:
        if not reference_audio.is_file():
            raise AgentError("manager_reference_missing", "Manager voice reference file is missing", retryable=False)
        if not 20 <= media_duration(reference_audio) <= 90:
            raise AgentError("manager_reference_duration_invalid", "Manager voice reference must be 20 to 90 seconds", retryable=False)
        temporary = self.paths.staging / "manager-reference.wav"
        try:
            self._normalize(reference_audio, temporary)
            embedding = self._load_embedding()(str(temporary))
            save_manager_embedding(self.paths.manager_embedding, self._vector(embedding))
        finally:
            temporary.unlink(missing_ok=True)

    def _transcribe_chunk(self, audio: Path) -> tuple[str, int, int]:
        try:
            result = self._load_gigaam().transcribe(str(audio), word_timestamps=True)
            text = (result.text if hasattr(result, "text") else str(result)).strip()
            words = list(getattr(result, "words", ()) or ())
            if not text:
                return "", 0, 0
            if not words:
                raise AgentError("gigaam_word_timestamps_missing", "GigaAM did not return word timestamps", retryable=True)
            starts = [float(word.start) for word in words if getattr(word, "start", None) is not None]
            ends = [float(word.end) for word in words if getattr(word, "end", None) is not None]
            if not starts or not ends:
                raise AgentError("gigaam_word_timestamps_missing", "GigaAM returned incomplete word timestamps", retryable=True)
            return text, round(min(starts) * 1000), round(max(ends) * 1000)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError("gigaam_transcription_failed", "GigaAM could not transcribe local audio", retryable=True) from exc

    def _diarize(self, audio: Path) -> list[tuple[int, int, str]]:
        output = self._load_diarization()(str(audio))
        turns: list[tuple[int, int, str]] = []
        diarization = getattr(output, "exclusive_speaker_diarization", None)
        if diarization is None:
            diarization = output.speaker_diarization
        iterator = diarization.itertracks(yield_label=True) if hasattr(diarization, "itertracks") else diarization
        for item in iterator:
            if len(item) == 3:
                turn, _track, speaker = item
            else:
                turn, speaker = item
            start, end = round(turn.start * 1000), round(turn.end * 1000)
            if end > start:
                turns.append((start, end, str(speaker)))
        return turns

    @staticmethod
    def _speaker_for(start: int, end: int, turns: list[tuple[int, int, str]]) -> str | None:
        overlaps: dict[str, int] = {}
        for turn_start, turn_end, speaker in turns:
            overlap = max(0, min(end, turn_end) - max(start, turn_start))
            if overlap:
                overlaps[speaker] = overlaps.get(speaker, 0) + overlap
        return max(overlaps, key=overlaps.get) if overlaps else None

    def _speaker_embeddings(self, audio: Path, turns: list[tuple[int, int, str]]) -> dict[str, list[float]]:
        longest: dict[str, tuple[int, int]] = {}
        for start, end, speaker in turns:
            if end - start > longest.get(speaker, (0, 0))[1] - longest.get(speaker, (0, 0))[0]:
                longest[speaker] = (start, end)
        inference = self._load_embedding()
        result: dict[str, list[float]] = {}
        for speaker, (start, end) in longest.items():
            if end - start < 1000:
                continue
            try:
                from pyannote.core import Segment
                vector = inference.crop(str(audio), Segment(start / 1000, end / 1000))
                result[speaker] = self._vector(vector)
            except Exception:
                continue
        return result

    @staticmethod
    def _vector(value: Any) -> list[float]:
        try:
            import numpy as np
            return np.asarray(value).reshape(-1).astype(float).tolist()
        except Exception as exc:
            raise AgentError("voice_embedding_invalid", "Voice embedding returned an invalid vector", retryable=True) from exc

    def transcribe(self, audio: Path) -> Transcript:
        duration = media_duration(audio)
        normalized = self.paths.staging / f"{audio.stem}.normalized.wav"
        chunks: list[Path] = []
        try:
            self._normalize(audio, normalized)
            turns = self._diarize(normalized)
            reference = load_manager_embedding(self.paths.manager_embedding)
            speaker_vectors = self._speaker_embeddings(normalized, turns) if reference else {}
            scores = {speaker: cosine(reference, vector) for speaker, vector in speaker_vectors.items()} if reference else {}
            known_speakers = sorted({speaker for _, _, speaker in turns})
            role_map = role_map_for_speakers(known_speakers, scores, self.config.manager_match_threshold)
            label_map = {speaker: f"Спикер {index + 1}" for index, speaker in enumerate(known_speakers)}
            segments: list[TranscriptSegment] = []
            for start in range(0, duration, CHUNK_SECONDS):
                chunk = self.paths.staging / f"{audio.stem}.{start:06d}.wav"
                chunks.append(chunk)
                chunk_duration = min(CHUNK_SECONDS, duration - start)
                self._normalize(normalized, chunk, start=start, duration=chunk_duration)
                text, first_word_ms, last_word_ms = self._transcribe_chunk(chunk)
                if not text:
                    continue
                started_ms = start * 1000 + first_word_ms
                ended_ms = min((start + chunk_duration) * 1000, start * 1000 + last_word_ms)
                if ended_ms <= started_ms:
                    ended_ms = min((start + chunk_duration) * 1000, started_ms + 1)
                speaker = self._speaker_for(started_ms, ended_ms, turns)
                role = role_map.get(speaker, "unknown")
                segments.append(TranscriptSegment(len(segments), started_ms, ended_ms, role, text, label_map.get(speaker)))
            if not segments:
                raise AgentError("empty_transcript", "GigaAM returned no text for local audio", retryable=True)
            text = "\n".join(f"[{segment.speaker_label or segment.role}]: {segment.text}" for segment in segments)
            return Transcript(text, segments)
        finally:
            normalized.unlink(missing_ok=True)
            for chunk in chunks:
                chunk.unlink(missing_ok=True)
