import logging
from pathlib import Path

import librosa
from onnx_asr import load_model

log = logging.getLogger(__name__)
TARGET_SR = 16_000
_model = None


def _get_model():
    global _model
    if _model is None:
        log.info("Loading GigaAM v3 ONNX")
        _model = load_model("gigaam-v3-e2e-rnnt")
    return _model


def transcribe_file(audio_path: str | None) -> str:
    if not audio_path:
        return ""
    path = Path(audio_path)
    if not path.is_file() or path.stat().st_size == 0:
        log.warning("Recording is missing or empty: %s", audio_path)
        return ""
    audio, _ = librosa.load(str(path), sr=TARGET_SR, mono=True)
    if audio.size == 0:
        return ""
    result = _get_model().recognize(audio)
    return (result.text if hasattr(result, "text") else str(result)).strip()


def build_transcript(customer_path: str | None, manager_path: str | None) -> tuple[str, list[dict]]:
    customer = transcribe_file(customer_path)
    manager = transcribe_file(manager_path)
    segments: list[dict] = []
    lines: list[str] = []
    if customer:
        segments.append({"speaker": "customer", "text": customer})
        lines.append(f"[Клиент]: {customer}")
    if manager:
        segments.append({"speaker": "manager", "text": manager})
        lines.append(f"[Менеджер]: {manager}")
    return "\n".join(lines), segments
