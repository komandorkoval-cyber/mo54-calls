"""Novofon Data API reconciliation for the mobile-first pilot.

Novofon webhooks are the low-latency path, but delivery can be delayed or
missed.  This module periodically asks the documented ``get.calls_report``
method for the recent window and puts *synthetic* events into the durable
``provider_events`` ledger.  It never modifies CRM calls directly: normal
worker processing remains the single apply path for both webhooks and reports.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

import httpx

from novofon import account_timezone, normalize_phone, parse_timestamp


log = logging.getLogger("mo54-worker")

DEFAULT_DATA_API_URL = "https://dataapi-jsonrpc.novofon.ru/v2.0"


class NovofonReconcileError(RuntimeError):
    """A safe, token-free error intended for worker logs."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _first(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _value(row: Mapping[str, Any], *names: str) -> object | None:
    """Look up a documented field in a report row and common nested blocks."""

    for block in (row, _mapping(row.get("call_info")), _mapping(row.get("contact_info"))):
        for name in names:
            if name in block and block[name] not in (None, ""):
                return block[name]
    return None


def _employee_block(row: Mapping[str, Any]) -> Mapping[str, Any]:
    # Data API exposes the answered employee both as a top-level ID and in an
    # optional employees list. Prefer the matching rich object when available.
    answered_id = _first(
        row.get("last_answered_employee_id"),
        row.get("first_answered_employee_id"),
        row.get("answered_employee_id"),
        row.get("employee_id"),
    )
    for name in (
        "answered_employee",
        "answer_employee",
        "employee_info",
        "employee",
        "manager",
        "user",
    ):
        candidate = _mapping(row.get(name))
        if candidate:
            return candidate
    employees = _as_list(row.get("employees"))
    if answered_id:
        for candidate in employees:
            candidate_mapping = _mapping(candidate)
            candidate_id = _first(
                candidate_mapping.get("employee_id"), candidate_mapping.get("id"), candidate_mapping.get("user_id")
            )
            if candidate_id == answered_id:
                return candidate_mapping
    for candidate in employees:
        candidate_mapping = _mapping(candidate)
        if candidate_mapping:
            return candidate_mapping
    return {}


def _employee_info(row: Mapping[str, Any]) -> dict[str, str]:
    employee = _employee_block(row)
    return {
        "employee_id": _first(
            employee.get("employee_id"), employee.get("id"), employee.get("user_id"),
            row.get("last_answered_employee_id"), row.get("first_answered_employee_id"),
            row.get("answered_employee_id"), row.get("employee_id"),
        )
        or "",
        "employee_full_name": _first(
            employee.get("employee_full_name"), employee.get("full_name"), employee.get("name"), row.get("employee_full_name")
        )
        or "",
        "extension_phone_number": _first(
            employee.get("extension_phone_number"), employee.get("extension"), employee.get("phone_number"), row.get("extension_phone_number")
        )
        or "",
    }


def _report_timestamp(row: Mapping[str, Any]) -> str | None:
    return _first(
        _value(
            row,
            "finish_date",
            "finish_time",
            "ended_at",
            "end_time",
            "call_end_time",
            "date_end",
            "notification_time",
            "start_date",
            "started_at",
        )
    )


def _call_session_id(row: Mapping[str, Any]) -> str | None:
    """Return Novofon's stable session identity, falling back to report ``id``.

    Data API's historical report format uses ``id`` for a call session.  Newer
    payload variants label it explicitly.  A row without either identifier is
    skipped instead of making up a non-idempotent call.
    """

    return _first(
        row.get("call_session_id"),
        row.get("session_id"),
        row.get("call_id"),
        row.get("id"),
    )


def _base_call_payload(row: Mapping[str, Any]) -> dict[str, Any] | None:
    session_id = _call_session_id(row)
    if not session_id:
        return None
    notification_time = _report_timestamp(row)
    talk_duration = _value(
        row,
        "talk_time_duration",
        "talk_duration",
        "talk_duration_sec",
        "talk_time",
        "duration",
    )
    total_duration = _value(
        row,
        "total_time_duration",
        "total_duration",
        "total_duration_sec",
        "total_time",
        "call_duration",
    )
    employee = _employee_info(row)
    payload: dict[str, Any] = {
        "event": "CALL_END",
        "call_session_id": session_id,
        "external_id": _first(row.get("call_api_external_id"), row.get("external_id"), row.get("request_id")),
        "virtual_phone_number": _first(
            _value(row, "virtual_phone_number", "virtual_number", "communication_number", "phone_number")
        ),
        "contact_info": {
            "contact_phone_number": _first(
                _value(row, "contact_phone_number", "contact_number", "client_phone_number", "client_phone", "phone")
            ),
            "communication_number": _first(
                _value(row, "communication_number", "virtual_phone_number", "virtual_number")
            ),
        },
        "employee_info": employee,
        "call_info": {
            "talk_time_duration": talk_duration if talk_duration is not None else 0,
            "total_time_duration": total_duration if total_duration is not None else 0,
            "call_status": _first(_value(row, "call_status", "status", "result", "call_result", "finish_reason")),
            "is_lost": _value(row, "is_lost"),
            "finish_reason": _first(_value(row, "finish_reason")),
        },
        "direction": _first(_value(row, "direction", "call_direction", "type")),
        "reconciled": True,
        "data_api_report_id": _first(row.get("id")),
    }
    if notification_time:
        payload["notification_time"] = notification_time
    return payload


def _recording_url(value: object) -> str | None:
    candidate = _first(value)
    if not candidate:
        return None
    parsed = urlparse(candidate)
    # The worker only stores provider links for later protected redirect; reject
    # arbitrary schemes and malformed links before they ever reach the ledger.
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return candidate


def _iter_recording_dicts(value: object, *, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    """Extract diverse documented/legacy report recording shapes conservatively."""

    if depth > 3 or value is None:
        return
    if isinstance(value, str):
        yield {"file_link": value}
        return
    if isinstance(value, Mapping):
        mapping = value
        if _first(
            mapping.get("file_link"),
            mapping.get("file_url"),
            mapping.get("recording_url"),
            mapping.get("url"),
            mapping.get("link"),
            mapping.get("full_record_file_link"),
        ):
            yield mapping
        for name in ("records", "recordings", "items", "data", "files", "call_records", "wav_call_records"):
            if name in mapping:
                yield from _iter_recording_dicts(mapping[name], depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_recording_dicts(item, depth=depth + 1)


def _recording_payloads(row: Mapping[str, Any], base: Mapping[str, Any]) -> Iterable[tuple[dict[str, Any], str]]:
    """Yield one canonical ``RECORD_CALL`` payload per unique HTTPS record URL."""

    candidates: list[object] = []
    for name in ("call_records", "wav_call_records", "recordings", "recording", "record_files"):
        if name in row:
            candidates.append(row[name])
    for name in ("full_record_file_link", "record_file_link", "recording_url", "file_link"):
        if row.get(name):
            candidates.append(row[name])

    seen: set[str] = set()
    for candidate in candidates:
        for record in _iter_recording_dicts(candidate):
            url = _recording_url(
                _first(
                    record.get("file_link"),
                    record.get("file_url"),
                    record.get("recording_url"),
                    record.get("url"),
                    record.get("link"),
                    record.get("full_record_file_link"),
                )
            )
            if not url or url in seen:
                continue
            seen.add(url)
            payload = dict(base)
            payload["event"] = "RECORD_CALL"
            payload["call_record_file_info"] = {
                "file_link": url,
                "file_id": _first(record.get("file_id"), record.get("id"), record.get("file_name"))
                or f"dataapi:{base['call_session_id']}:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}",
                "file_duration": _first(
                    record.get("file_duration"),
                    record.get("call_record_duration"),
                    record.get("duration"),
                    row.get("call_record_duration"),
                    row.get("talk_duration"),
                )
                or 0,
            }
            yield payload, url


def report_row_events(row: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any], str | None]]:
    """Turn one Data API report row into deterministic ledger events.

    Returns tuples of ``event_type, idempotency_key, payload, recording_url``.
    The idempotency keys deliberately do not include window timestamps, making
    the overlapping reconciliation window safe to run repeatedly.
    """

    base = _base_call_payload(row)
    if not base:
        return []
    session_id = str(base["call_session_id"])
    events: list[tuple[str, str, dict[str, Any], str | None]] = [
        ("CALL_END", f"novofon:dataapi:call-end:{session_id}", base, None)
    ]
    for payload, recording_url in _recording_payloads(row, base):
        link_digest = hashlib.sha256(recording_url.encode("utf-8")).hexdigest()
        events.append(
            (
                "RECORD_CALL",
                f"novofon:dataapi:record:{session_id}:{link_digest}",
                payload,
                recording_url,
            )
        )
    return events


def _records_from_response(response: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], int | None]:
    """Support documented Data API response variants without accepting junk."""

    if response.get("error"):
        error = _mapping(response.get("error"))
        code = _first(error.get("code")) or "unknown"
        raise NovofonReconcileError(f"Data API returned an error (code {code})")
    result: object = response.get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, Mapping)], None
    result_mapping = _mapping(result)
    for key in ("data", "calls", "items", "records", "result"):
        rows = result_mapping.get(key)
        if isinstance(rows, list):
            total = _first(
                result_mapping.get("total"),
                result_mapping.get("total_count"),
                result_mapping.get("count"),
            )
            try:
                return [row for row in rows if isinstance(row, Mapping)], int(total) if total is not None else None
            except (TypeError, ValueError):
                return [row for row in rows if isinstance(row, Mapping)], None
    # Some versions return the records as the complete result object only when
    # it is explicitly marked with one call.  Treat any other shape as an API
    # incompatibility rather than marking an empty reconciliation successful.
    if any(key in result_mapping for key in ("id", "call_session_id", "contact_phone_number")):
        return [result_mapping], 1
    raise NovofonReconcileError("Data API response does not contain a calls list")


class NovofonReconciler:
    """Rate-controlled, safe-to-fail Novofon Data API reconciler."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        api_url: str | None = None,
        interval_seconds: int | None = None,
        window_hours: int | None = None,
        overlap_minutes: int | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
        virtual_phone: str | None = None,
        enabled: bool | None = None,
        enqueue: Callable[..., bool] | None = None,
    ) -> None:
        env_interval = os.environ.get("NOVOFON_RECONCILE_INTERVAL_SECONDS") or os.environ.get(
            "NOVOFON_RECONCILE_INTERVAL_SEC"
        )
        self.access_token = access_token if access_token is not None else os.environ.get("NOVOFON_ACCESS_TOKEN")
        self.api_url = api_url or os.environ.get("NOVOFON_DATA_API_URL", DEFAULT_DATA_API_URL)
        self.virtual_phone = normalize_phone(
            virtual_phone if virtual_phone is not None else os.environ.get("NOVOFON_VIRTUAL_NUMBER", "")
        )
        self.interval_seconds = interval_seconds or _positive_int(env_interval, 3600, minimum=30, maximum=86400)
        self.window_hours = window_hours or _positive_int(
            os.environ.get("NOVOFON_RECONCILE_WINDOW_HOURS"), 24, minimum=1, maximum=168
        )
        self.overlap_minutes = overlap_minutes if overlap_minutes is not None else _positive_int(
            os.environ.get("NOVOFON_RECONCILE_OVERLAP_MINUTES"), 10, minimum=0, maximum=1440
        )
        self.page_size = page_size or _positive_int(
            os.environ.get("NOVOFON_RECONCILE_PAGE_SIZE"), 100, minimum=1, maximum=500
        )
        self.max_pages = max_pages or _positive_int(
            os.environ.get("NOVOFON_RECONCILE_MAX_PAGES"), 50, minimum=1, maximum=200
        )
        self.enabled = _truthy(os.environ.get("NOVOFON_RECONCILE_ENABLED")) if enabled is None else enabled
        # Keep report parsing independently testable and avoid opening/importing
        # database code until a real reconciliation actually enqueues an event.
        if enqueue is None:
            def enqueue(**event: Any) -> bool:
                from db import enqueue_reconciled_novofon_event

                return enqueue_reconciled_novofon_event(**event)
        self.enqueue = enqueue
        self.next_run_at = 0.0
        self.failure_count = 0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.access_token and self.virtual_phone and self._safe_api_url())

    def _safe_api_url(self) -> bool:
        parsed = urlparse(self.api_url)
        return parsed.scheme == "https" and bool(parsed.hostname)

    def _mark_success(self, now: float) -> None:
        self.failure_count = 0
        self.next_run_at = now + self.interval_seconds

    def _mark_failure(self, now: float) -> None:
        self.failure_count += 1
        # Retry a failed reconciliation soon enough to protect the 24h window,
        # but never hammer an unavailable provider.  A normal interval remains
        # the ceiling, and no error text can expose the access token.
        backoff = min(
            self.interval_seconds,
            max(60, 30 * (2 ** min(self.failure_count - 1, 8))),
        )
        self.next_run_at = now + backoff

    def maybe_reconcile(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now < self.next_run_at:
            return
        if not self.access_token:
            # A deliberately disabled/misconfigured integration is not an
            # exception loop.  Check again on the normal cadence after a
            # configuration update/container restart.
            log.warning("Novofon Data API reconciliation is enabled but no access token is configured")
            self._mark_success(now)
            return
        if not self.virtual_phone:
            log.warning("Novofon Data API reconciliation is enabled but no virtual phone is configured")
            self._mark_success(now)
            return
        if not self._safe_api_url():
            log.error("Novofon Data API reconciliation has an invalid HTTPS endpoint")
            self._mark_failure(now)
            return
        try:
            created, observed = self.reconcile()
            self._mark_success(now)
            log.info("Novofon Data API reconciliation observed %d calls and queued %d events", observed, created)
        except Exception as exc:  # A provider outage must never stop call/event processing.
            self._mark_failure(now)
            # Do not include exception text: HTTP clients and provider errors
            # can echo request bodies, which include the access token.
            log.warning("Novofon Data API reconciliation failed (%s); retry is scheduled", type(exc).__name__)

    def reconcile(self, *, now: datetime | None = None) -> tuple[int, int]:
        """Fetch the overlapping recent window and enqueue unseen provider events.

        Returns ``(created_events, observed_call_rows)``.  This method raises
        token-free errors to its caller; :meth:`maybe_reconcile` contains them.
        """

        if not self.access_token:
            raise NovofonReconcileError("Novofon access token is not configured")
        if not self._safe_api_url():
            raise NovofonReconcileError("Novofon Data API endpoint must use HTTPS")
        # Novofon's documented `date_from`/`date_till` parameters have no
        # offset and are interpreted in the account timezone.
        end = (now or datetime.now(timezone.utc)).astimezone(account_timezone())
        start = end - timedelta(hours=self.window_hours, minutes=self.overlap_minutes)
        date_from = start.strftime("%Y-%m-%d %H:%M:%S")
        date_till = end.strftime("%Y-%m-%d %H:%M:%S")
        created = 0
        observed = 0
        offset = 0
        request_id = str(uuid.uuid4())

        timeout = httpx.Timeout(20.0, connect=10.0)
        with httpx.Client(timeout=timeout, follow_redirects=False, verify=True) as client:
            for page in range(self.max_pages):
                request_body = {
                    "jsonrpc": "2.0",
                    "id": f"crm-reconcile-{request_id}-{page}",
                    "method": "get.calls_report",
                    "params": {
                        "access_token": self.access_token,
                        "date_from": date_from,
                        "date_till": date_till,
                        "limit": self.page_size,
                        "offset": offset,
                    },
                }
                try:
                    response = client.post(
                        self.api_url,
                        json=request_body,
                        headers={"Content-Type": "application/json; charset=UTF-8"},
                    )
                    response.raise_for_status()
                    parsed = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise NovofonReconcileError("Data API request failed") from exc
                if not isinstance(parsed, Mapping):
                    raise NovofonReconcileError("Data API returned a non-object response")
                records, total = _records_from_response(parsed)
                observed += len(records)
                for row in records:
                    if not self._matches_virtual_phone(row):
                        continue
                    for event_type, key, payload, recording_url in report_row_events(row):
                        occurred_at = parse_timestamp(payload.get("notification_time"))
                        if self.enqueue(
                            event_type=event_type,
                            call_session_id=str(payload["call_session_id"]),
                            payload=payload,
                            idempotency_key=key,
                            occurred_at=occurred_at,
                            recording_url=recording_url,
                        ):
                            created += 1
                offset += len(records)
                if not records or len(records) < self.page_size:
                    break
                if total is not None and offset >= total:
                    break
            else:
                # We deliberately cap pages so a provider pagination bug cannot
                # hold the call worker forever.  Next run resumes the same safe
                # overlapping window and idempotency makes it harmless.
                log.warning("Novofon Data API reconciliation reached its page limit")
        return created, observed

    def _matches_virtual_phone(self, row: Mapping[str, Any]) -> bool:
        """Fail closed so a shared Novofon account cannot import other lines."""

        if not self.virtual_phone:
            return False
        candidates = (
            _value(row, "virtual_phone_number", "virtual_number", "communication_number"),
            _mapping(row.get("contact_info")).get("communication_number"),
        )
        return any(normalize_phone(value) == self.virtual_phone for value in candidates if value not in (None, ""))
