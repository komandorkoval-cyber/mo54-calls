"""Small, deliberately strict Novofon integration helpers.

The CRM accepts webhooks durably first and lets the worker interpret them.
This module only contains deterministic parsing and the one user-initiated
Call API request; it never downloads provider recordings or disables TLS.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


CALL_API_URL = "https://callapi-jsonrpc.novofon.ru/v4.0"
DATA_API_URL = "https://dataapi-jsonrpc.novofon.ru/v2.0"
INTERACTIVE_MEDIA_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp3\Z", re.IGNORECASE)


class NovofonAPIError(RuntimeError):
    """An explicit Call API failure safe to show as a generic CRM error."""


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_phone(value: Any) -> str | None:
    """Return E.164 digits without '+' or None when there is no usable number."""
    number = digits(value)
    if len(number) == 11 and number[0] in {"7", "8"}:
        return "7" + number[-10:]
    if 7 <= len(number) <= 15:
        return number
    return None


def interactive_call_route(forward_phone: Any, operator_media: Any) -> dict[str, list[str] | str]:
    """Build a strictly static Novofon interactive-call routing instruction.

    The provider owns the media file. We only name a safe MP3 from its File
    Base and return one configured employee phone, without accepting or
    persisting caller data.
    """
    phone = normalize_phone(forward_phone)
    media = str(operator_media or "").strip()
    if not phone:
        raise ValueError("Interactive Novofon route requires a valid employee phone")
    if not INTERACTIVE_MEDIA_FILENAME.fullmatch(media):
        raise ValueError("Interactive Novofon operator media must be a safe MP3 filename")
    return {"phones": [phone], "operator_media": media}


def normalize_employee_id(value: Any) -> int | None:
    """Return Novofon's positive numeric employee ID, or ``None``.

    The Call API deliberately defines ``employee.id`` as a JSON number rather
    than a string.  Keeping the conversion here means a bad CRM mapping fails
    before a callback request is sent, and prevents a subtle API contract
    mismatch for otherwise valid-looking values such as ``"100"``.
    """
    raw = str(value or "").strip()
    if not re.fullmatch(r"[1-9]\d*", raw):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def event_type(payload: dict[str, Any]) -> str:
    # Novofon recording notifications can contain only the recording link,
    # without an explicit event/type field. Treat that shape as RECORD_CALL
    # so the API ledger and worker follow the same idempotent path.
    if recording_url(payload):
        return "RECORD_CALL"
    raw = str(payload.get("event") or payload.get("event_type") or "").upper().strip()
    aliases = {
        "CALL_END": "CALL_END",
        "CALLEND": "CALL_END",
        "RECORD_CALL": "RECORD_CALL",
        "RECORDCALL": "RECORD_CALL",
        "CALL_RECORD": "RECORD_CALL",
    }
    return aliases.get(raw, raw or "UNKNOWN")


def nested(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def call_session_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("call_session_id") or nested(payload, "call_info", "call_session_id")
    return str(value) if value not in (None, "") else None


def recording_url(payload: dict[str, Any]) -> str | None:
    candidates = (
        nested(payload, "call_record_file_info", "file_link"),
        nested(payload, "call_record_file_info", "record_file_link"),
        nested(payload, "recording", "file_link"),
        payload.get("file_link"),
        payload.get("recording_url"),
    )
    for value in candidates:
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def link_hash(url: str | None) -> str | None:
    return hashlib.sha256(url.encode("utf-8")).hexdigest() if url else None


def event_idempotency_key(payload: dict[str, Any]) -> str:
    """Stable identity for a delivery even when Novofon retries or reorders it."""
    supplied = payload.get("delivery_id") or payload.get("event_id") or payload.get("notification_id")
    if supplied not in (None, ""):
        return f"novofon:delivery:{supplied}"
    source = {
        "event": event_type(payload),
        "session": call_session_id(payload),
        "recording": link_hash(recording_url(payload)),
        "time": payload.get("notification_time") or payload.get("created_at"),
        "external": payload.get("external_id"),
    }
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "novofon:payload:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def public_event_view(payload: dict[str, Any]) -> dict[str, Any]:
    """A compact log-safe summary; raw payload remains in the event ledger only."""
    return {
        "event_type": event_type(payload),
        "call_session_id": call_session_id(payload),
        "direction": payload.get("direction"),
        "external_id": payload.get("external_id"),
        "virtual_phone_number": payload.get("virtual_phone_number"),
    }


@dataclass(frozen=True)
class NovofonClient:
    access_token: str
    virtual_number: str
    call_api_url: str = CALL_API_URL
    timeout_seconds: float = 15.0

    @property
    def enabled(self) -> bool:
        return bool(self.access_token and normalize_phone(self.virtual_number))

    async def start_employee_call(
        self,
        *,
        request_id: str,
        employee_id: str,
        employee_phone: str,
        contact_phone: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise NovofonAPIError("Интеграция Call API Novofon пока не настроена")
        destination = normalize_phone(contact_phone)
        manager = normalize_phone(employee_phone)
        provider_employee_id = normalize_employee_id(employee_id)
        if not destination or not manager or provider_employee_id is None:
            raise NovofonAPIError("Не заполнены телефон клиента или данные сотрудника Novofon")

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "start.employee_call",
            "params": {
                "access_token": self.access_token,
                "first_call": "employee",
                "virtual_phone_number": normalize_phone(self.virtual_number),
                "show_virtual_phone_number": True,
                "contact": destination,
                "external_id": request_id,
                "direction": "out",
                "employee": {"id": provider_employee_id, "phone_number": manager},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.call_api_url,
                    json=payload,
                    headers={"Content-Type": "application/json; charset=UTF-8"},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # A timeout can mean Novofon accepted the callback.  The caller marks
            # that request as unknown, so it is never blindly retried.
            raise NovofonAPIError("Не удалось подтвердить callback в Novofon") from exc
        if not isinstance(data, dict):
            raise NovofonAPIError("Call API Novofon вернул некорректный ответ")
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else None
            message = message or "Call API Novofon вернул ошибку"
            raise NovofonAPIError(str(message))
        result = data.get("result")
        if not isinstance(result, dict):
            raise NovofonAPIError("Call API Novofon вернул неполный ответ")
        return {"request": payload, "response": data, "result": result}
