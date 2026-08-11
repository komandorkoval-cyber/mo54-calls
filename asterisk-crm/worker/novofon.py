"""Small, dependency-free normalizer for Novofon webhook payloads.

Novofon sends JSON notifications directly to the API.  The API deliberately
stores the original payload first; this module converts the parts needed by
the asynchronous worker into a stable representation.  Keeping this logic
outside of the HTTP layer also makes replaying a saved provider event safe.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Novofon documents its unqualified date/time fields as
# ``YYYY-MM-DD HH:MM:SS``.  Those values are expressed in the account's
# timezone, not implicitly in UTC.  Keep one configurable default for both
# webhook parsing and Data API reconciliation.
DEFAULT_ACCOUNT_TIMEZONE = "Europe/Moscow"


def _as_dict(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _first(*values: object) -> str | None:
    for value in values:
        result = _text(value)
        if result:
            return result
    return None


def normalize_phone(value: object) -> str | None:
    """Return an E.164-ish phone number, or ``None`` for an extension/empty value.

    Novofon is a Russian provider, therefore a ten-digit local number is
    interpreted as Russia.  We intentionally do not invent a contact for a
    short internal extension such as ``100``.
    """

    raw = _text(value)
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) < 11:
        return None
    return f"+{digits}"


def parse_duration(value: object) -> int:
    """Accept Novofon's numeric and human/API duration formats."""

    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    raw = str(value).strip()
    try:
        return max(0, int(float(raw)))
    except ValueError:
        pass
    parts = raw.split(":")
    if 2 <= len(parts) <= 3 and all(part.strip().isdigit() for part in parts):
        values = [int(part.strip()) for part in parts]
        if len(values) == 2:
            return values[0] * 60 + values[1]
        return values[0] * 3600 + values[1] * 60 + values[2]
    match = re.search(r"\d+", raw)
    return int(match.group(0)) if match else 0


def account_timezone(name: str | None = None) -> ZoneInfo:
    """Return the configured Novofon account timezone.

    An explicit offset in an incoming timestamp remains authoritative.  This
    helper is used only for the provider's documented timezone-less values.
    Invalid configuration must be visible to the worker instead of silently
    shifting data to UTC.
    """

    configured = (name or os.environ.get("NOVOFON_ACCOUNT_TIMEZONE") or DEFAULT_ACCOUNT_TIMEZONE).strip()
    try:
        return ZoneInfo(configured)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid NOVOFON_ACCOUNT_TIMEZONE: {configured!r}") from exc


def parse_timestamp(value: object, *, account_timezone_name: str | None = None) -> datetime | None:
    """Parse documented timestamps in the Novofon account timezone.

    Novofon may send ISO-8601 values with an offset or a timezone-less
    ``YYYY-MM-DD HH:MM:SS``.  Only the latter receives the configured account
    timezone; offset-aware values are never rewritten.
    """

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=account_timezone)
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=account_timezone(account_timezone_name))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=account_timezone(account_timezone_name))
        except ValueError:
            continue
    return None


def normalize_direction(value: object) -> str:
    raw = (_text(value) or "").lower()
    if raw in {"out", "outbound", "outgoing", "исходящий", "исходящие"}:
        return "out"
    return "in"


def canonical_event_type(payload: Mapping[str, Any]) -> str:
    raw = _first(
        payload.get("event"),
        payload.get("event_type"),
        payload.get("notification_type"),
        payload.get("type"),
    )
    value = (raw or "").strip().upper()
    if value in {"CALL_END", "CALL_ENDED", "ЗАВЕРШЕНИЕ ЗВОНКА"}:
        return "CALL_END"
    if value in {"RECORD_CALL", "CALL_RECORD", "RECORDED_CALL", "ЗАПИСАННЫЙ РАЗГОВОР"}:
        return "RECORD_CALL"
    return value or "UNKNOWN"


_ANSWERED_STATUSES = {
    "answered",
    "answer",
    "completed",
    "complete",
    "connected",
    "success",
    "успешно",
    "отвечен",
    "отвечено",
}
_MISSED_STATUSES = {
    "missed",
    "no_answer",
    "noanswer",
    "not_answered",
    "unanswered",
    "busy",
    "cancelled",
    "canceled",
    "failed",
    "не отвечен",
    "неотвечен",
    "пропущен",
}


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = (_text(value) or "").lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _is_answered(status: str | None, talk_duration_sec: int, is_lost: bool | None) -> bool:
    # ``is_lost`` is the canonical Data API field.  It is more reliable than
    # inferring a missed call from a zero duration, which can also happen in
    # non-lost technical call endings.
    if is_lost is True:
        return False
    if is_lost is False:
        return True
    if talk_duration_sec > 0:
        return True
    normalized = (status or "").strip().lower()
    if normalized in _ANSWERED_STATUSES:
        return True
    if normalized in _MISSED_STATUSES:
        return False
    return False


@dataclass(frozen=True)
class NovofonEvent:
    event_type: str
    call_session_id: str | None
    external_id: str | None
    occurred_at: datetime | None
    direction: str
    direction_known: bool
    contact_phone: str | None
    virtual_phone: str | None
    employee_id: str | None
    employee_extension: str | None
    employee_name: str | None
    talk_duration_sec: int
    total_duration_sec: int
    call_status: str | None
    is_lost: bool | None
    answered: bool
    recording_url: str | None
    recording_id: str | None
    recording_duration_sec: int

    @property
    def started_at(self) -> datetime | None:
        if not self.occurred_at:
            return None
        return self.occurred_at - timedelta(seconds=max(self.total_duration_sec, self.talk_duration_sec))

    @property
    def missed_inbound(self) -> bool:
        # Never create an automatic callback task from a partial notification
        # whose direction is absent.  Data API reconciliation occasionally has
        # older rows without that field; treating the parser's "in" fallback as
        # fact would create a false missed-call task.
        return (
            self.event_type == "CALL_END"
            and self.direction_known
            and self.direction == "in"
            and not self.answered
        )

    @property
    def recording_link_hash(self) -> str | None:
        if not self.recording_url:
            return None
        return hashlib.sha256(self.recording_url.encode("utf-8")).hexdigest()

    @property
    def provider_recording_id(self) -> str | None:
        """Prefer Novofon's stable recording ID over a potentially signed URL."""

        return self.recording_id or self.recording_link_hash


def parse_event(payload: Mapping[str, Any]) -> NovofonEvent:
    """Build a stable event from documented Novofon payload variants."""

    call_info = _as_dict(payload.get("call_info"))
    contact_info = _as_dict(payload.get("contact_info"))
    employee_info = _as_dict(payload.get("employee_info"))
    record_info = _as_dict(
        payload.get("call_record_file_info")
        or payload.get("record_file_info")
        or payload.get("recording_info")
    )

    event_type = canonical_event_type(payload)
    call_session_id = _first(
        payload.get("call_session_id"), call_info.get("call_session_id"), payload.get("session_id")
    )
    external_id = _first(payload.get("external_id"), call_info.get("external_id"))
    direction_value = _first(payload.get("direction"), call_info.get("direction"))
    direction = normalize_direction(direction_value)
    contact_phone = normalize_phone(
        _first(
            contact_info.get("contact_phone_number"),
            contact_info.get("phone_number"),
            contact_info.get("phone"),
            payload.get("contact_phone_number"),
        )
    )
    virtual_phone = normalize_phone(
        _first(
            payload.get("virtual_phone_number"),
            contact_info.get("communication_number"),
            payload.get("communication_number"),
        )
    )
    employee_id = _first(employee_info.get("employee_id"), employee_info.get("id"), payload.get("employee_id"))
    employee_extension = _first(
        employee_info.get("extension_phone_number"),
        employee_info.get("extension"),
        payload.get("extension_phone_number"),
    )
    employee_name = _first(employee_info.get("employee_full_name"), employee_info.get("name"))
    talk_duration_sec = parse_duration(
        _first(call_info.get("talk_time_duration"), call_info.get("talk_duration"), payload.get("talk_time_duration"))
    )
    total_duration_sec = parse_duration(
        _first(call_info.get("total_time_duration"), call_info.get("total_duration"), payload.get("total_time_duration"))
    )
    call_status = _first(
        call_info.get("call_status"),
        payload.get("call_status"),
        call_info.get("finish_reason"),
        payload.get("finish_reason"),
        payload.get("status"),
    )
    is_lost = _as_bool(_first(call_info.get("is_lost"), payload.get("is_lost")))
    recording_url = _first(
        record_info.get("file_link"),
        record_info.get("file_url"),
        record_info.get("recording_url"),
        payload.get("file_link"),
        payload.get("recording_url"),
    )
    recording_id = _first(
        record_info.get("file_id"),
        record_info.get("id"),
        record_info.get("file_name"),
        payload.get("recording_id"),
    )
    recording_duration_sec = parse_duration(
        _first(
            record_info.get("file_duration"),
            record_info.get("call_record_duration"),
            record_info.get("duration"),
            payload.get("call_record_duration"),
        )
    )
    if not recording_id and call_session_id and recording_url:
        recording_id = f"{call_session_id}:{hashlib.sha256(recording_url.encode('utf-8')).hexdigest()[:24]}"

    return NovofonEvent(
        event_type=event_type,
        call_session_id=call_session_id,
        external_id=external_id,
        occurred_at=parse_timestamp(_first(payload.get("notification_time"), payload.get("occurred_at"))),
        direction=direction,
        direction_known=direction_value is not None,
        contact_phone=contact_phone,
        virtual_phone=virtual_phone,
        employee_id=employee_id,
        employee_extension=employee_extension,
        employee_name=employee_name,
        talk_duration_sec=talk_duration_sec,
        total_duration_sec=total_duration_sec,
        call_status=call_status,
        is_lost=is_lost,
        answered=_is_answered(call_status, talk_duration_sec, is_lost),
        recording_url=recording_url,
        recording_id=recording_id,
        recording_duration_sec=recording_duration_sec,
    )
