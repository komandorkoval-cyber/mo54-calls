import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from novofon import NovofonEvent, normalize_phone, parse_event

DATABASE_URL = os.environ["DATABASE_URL"]

AI_DEAL_UPDATE_FIELDS = (
    "qualification_segment", "estimated_budget_min", "estimated_budget_max", "budget_range",
    "pain_primary", "pain_secondary", "customer_quote", "decision_makers", "decision_maker_status",
    "alternative_considered", "alternative_reason", "desired_install_date", "desired_install_period",
    "next_contact_at", "suggested_stage",
)
DEAL_SNAPSHOT_FIELDS = tuple("stage" if field == "suggested_stage" else field for field in AI_DEAL_UPDATE_FIELDS)


def _json_safe(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _commercial_fields(insight: dict) -> dict:
    commercial = insight.get("commercial_proposal") or {}
    fields = commercial.get("fields") or {}
    return fields if isinstance(fields, dict) else {}


def _actionable(proposal: dict | None) -> bool:
    if not isinstance(proposal, dict) or proposal.get("inference_status") not in {"supported", "inferred"}:
        return False
    value = proposal.get("proposed_value")
    return value not in (None, [], "unknown")


def build_deal_update_payload(insight: dict, deal_snapshot: dict) -> tuple[dict, list[dict]] | None:
    """Build a reviewable, field-level proposal; never a business mutation."""

    fields = _commercial_fields(insight)
    if not any(_actionable(fields.get(field)) for field in AI_DEAL_UPDATE_FIELDS):
        return None
    normalized_fields = {field: fields[field] for field in AI_DEAL_UPDATE_FIELDS if field in fields}
    if len(normalized_fields) != len(AI_DEAL_UPDATE_FIELDS):
        return None
    evidence = [
        {"field": field, **item, "inference_status": proposal["inference_status"]}
        for field, proposal in normalized_fields.items()
        for item in proposal.get("evidence", [])
    ]
    return ({
        "proposed_fields": _json_safe(normalized_fields),
        "base_values": _json_safe({field: deal_snapshot.get(field) for field in DEAL_SNAPSHOT_FIELDS}),
    }, evidence)


def _deal_snapshots(cursor, where_sql: str, params: tuple) -> list[dict]:
    columns = ("id", *DEAL_SNAPSHOT_FIELDS)
    projection = ",".join("d.stage::text AS stage" if field == "stage" else f"d.{field}" for field in columns)
    cursor.execute(f"SELECT {projection} {where_sql}", params)
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def unambiguous_accessible_deal_for_call(cursor, call_id: int) -> dict | None:
    """Use existing call/contact/deal relations; do not guess when more than one fits."""

    direct = _deal_snapshots(
        cursor,
        """FROM call_deals cd JOIN calls c ON c.id=cd.call_id JOIN deals d ON d.id=cd.deal_id
           WHERE cd.call_id=%s AND d.owner_id=c.owner_id""",
        (call_id,),
    )
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        return None
    related = _deal_snapshots(
        cursor,
        """FROM calls c JOIN deals d ON d.contact_id=c.contact_id
           WHERE c.id=%s AND d.owner_id=c.owner_id""",
        (call_id,),
    )
    return related[0] if len(related) == 1 else None


@contextmanager
def connection():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ingest(job: dict) -> int | None:
    started = job.get("started_at")
    if isinstance(started, (int, float)):
        started = datetime.fromtimestamp(started, tz=timezone.utc)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ingest_asterisk_call(%s,%s,%s,%s,%s,%s,%s,%s)",
            (job["call_id"], job.get("direction", "in"), job.get("caller_number"),
             job.get("callee_number"), started, int(job.get("duration_sec") or 0),
             job.get("recording_rx"), job.get("recording_tx")),
        )
        row = cur.fetchone()
        return row[0] if row else None


def recover_stale_jobs(minutes: int = 15) -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE processing_jobs SET status='retry',locked_at=NULL,locked_by=NULL,
               next_attempt_at=now(),last_error='Worker lease expired',updated_at=now()
               WHERE status='running' AND locked_at < now()-(%s * interval '1 minute')""",
            (minutes,),
        )
        return cur.rowcount


def claim_job(worker_id: str) -> dict | None:
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """WITH candidate AS (
                 SELECT id FROM processing_jobs
                 WHERE status IN ('pending','retry') AND next_attempt_at <= now()
                   AND attempts < max_attempts
                 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
               )
               UPDATE processing_jobs j SET status='running',locked_at=now(),locked_by=%s,
                 attempts=attempts+1,updated_at=now()
               FROM candidate WHERE j.id=candidate.id RETURNING j.*""",
            (worker_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def call_context(call_id: int) -> dict:
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT c.*,r1.storage_path AS customer_path,r2.storage_path AS manager_path
               FROM calls c
               LEFT JOIN recordings r1 ON r1.call_id=c.id AND r1.channel='customer'
               LEFT JOIN recordings r2 ON r2.call_id=c.id AND r2.channel='manager'
               WHERE c.id=%s""",
            (call_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Call {call_id} not found")
        return dict(row)


def latest_transcript(call_id: int) -> dict | None:
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM transcripts WHERE call_id=%s ORDER BY version DESC LIMIT 1", (call_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_transcript(call_id: int, text: str, segments: list[dict], model: str) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version),0)+1 FROM transcripts WHERE call_id=%s", (call_id,))
        version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO transcripts(call_id,version,text,asr_model) VALUES(%s,%s,%s,%s) RETURNING id",
            (call_id, version, text, model),
        )
        transcript_id = cur.fetchone()[0]
        for index, segment in enumerate(segments):
            cur.execute(
                """INSERT INTO transcript_segments(transcript_id,speaker,started_ms,ended_ms,text,ordinal)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (transcript_id, segment["speaker"], segment.get("started_ms"),
                 segment.get("ended_ms"), segment["text"], index),
            )
        cur.execute(
            """UPDATE calls SET transcript=%s,processing_status='analyzing',status='transcribed',
               processing_error=NULL,updated_at=now() WHERE id=%s""",
            (text, call_id),
        )
        cur.execute(
            """INSERT INTO processing_jobs(call_id,kind,status) VALUES(%s,'analyze','pending')
               ON CONFLICT(call_id,kind) DO UPDATE SET status='pending',next_attempt_at=now(),
                 attempts=0,last_error=NULL,updated_at=now()""",
            (call_id,),
        )


def save_insight(call_id: int, insight: dict, model: str, prompt_version: str) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version),0)+1 FROM call_insights WHERE call_id=%s", (call_id,))
        version = cur.fetchone()[0]
        cur.execute("UPDATE call_insights SET is_current=false WHERE call_id=%s", (call_id,))
        cur.execute(
            """INSERT INTO call_insights(call_id,version,prompt_version,model,data,confidence)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
            (call_id, version, prompt_version, model, Json(insight), insight.get("confidence")),
        )
        insight_id = cur.fetchone()[0]
        cur.execute(
            """UPDATE calls SET theme=%s,client_request=%s,agreements=%s,amount=%s,
               next_step=%s,next_date=%s,raw_json=%s,status='stored',
               processing_status='ready',processing_error=NULL,updated_at=now() WHERE id=%s""",
            (insight.get("summary"), insight.get("customer_need"),
             json.dumps(insight.get("agreements"), ensure_ascii=False),
             insight.get("budget_amount"), insight.get("next_step"),
             insight.get("next_step_date"), Json(insight), call_id),
        )
        fields = _commercial_fields(insight)
        target_deal = unambiguous_accessible_deal_for_call(cur, call_id)
        update_draft = build_deal_update_payload(insight, target_deal) if target_deal else None
        if target_deal:
            if update_draft:
                payload, evidence = update_draft
                cur.execute(
                    """INSERT INTO ai_action_drafts(
                           call_id,insight_id,kind,target_deal_id,payload,evidence,base_deal_snapshot,proposal_schema_version
                       ) VALUES(%s,%s,'deal_update',%s,%s,%s,%s,'p0-deal-update-v1')
                       ON CONFLICT(insight_id,kind) DO NOTHING""",
                    (call_id, insight_id, target_deal["id"], Json(payload), Json(evidence),
                     Json(payload["base_values"])),
                )
        elif insight.get("product") and any(_actionable(fields.get(field)) for field in AI_DEAL_UPDATE_FIELDS):
            qualification = fields["qualification_segment"].get("proposed_value") if _actionable(fields.get("qualification_segment")) else "unknown"
            suggested_stage = fields["suggested_stage"].get("proposed_value") if _actionable(fields.get("suggested_stage")) else "new_lead"
            evidence = [
                {"field": field, **item, "inference_status": proposal["inference_status"]}
                for field, proposal in fields.items() if isinstance(proposal, dict)
                for item in proposal.get("evidence", [])
            ]
            cur.execute(
                """INSERT INTO ai_action_drafts(call_id,insight_id,kind,payload,evidence)
                   VALUES(%s,%s,'deal_create',%s,%s) ON CONFLICT(insight_id,kind) DO NOTHING""",
                (call_id, insight_id, Json({"title": insight["product"], "stage": suggested_stage or "new_lead",
                                            "qualification_segment": qualification or "unknown"}), Json(evidence)),
            )


def complete_job(job_id: str, duration_ms: int) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE processing_jobs SET status='completed',duration_ms=%s,locked_at=NULL,
               locked_by=NULL,updated_at=now() WHERE id=%s""",
            (duration_ms, job_id),
        )


def fail_job(job: dict, error: str) -> None:
    terminal = job["attempts"] >= job["max_attempts"]
    delay = min(900, 2 ** min(job["attempts"], 9))
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE processing_jobs SET status=%s,last_error=%s,locked_at=NULL,locked_by=NULL,
               next_attempt_at=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s""",
            ("failed" if terminal else "retry", error[:4000], delay, job["id"]),
        )
        cur.execute(
            "UPDATE calls SET processing_status='failed',processing_error=%s,updated_at=now() WHERE id=%s",
            (error[:4000], job["call_id"]),
        )


# ---------------------------------------------------------------------------
# Novofon provider event queue
# ---------------------------------------------------------------------------


def recover_stale_novofon_events(minutes: int = 15) -> int:
    """Release provider-event leases after an interrupted worker run."""

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE provider_events
               SET processing_status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retry' END,
                   locked_at=NULL, locked_by=NULL,
                   next_attempt_at=now(),
                   processing_error=coalesce(processing_error, 'Worker lease expired'),
                   updated_at=now()
               WHERE source IN ('novofon','novofon-data-api') AND processing_status='running'
                 AND locked_at < now()-(%s * interval '1 minute')""",
            (minutes,),
        )
        return cur.rowcount


def claim_novofon_event(worker_id: str) -> dict | None:
    """Claim one durable Novofon event without blocking another worker."""

    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """WITH candidate AS (
                 SELECT id FROM provider_events
                 WHERE source IN ('novofon','novofon-data-api')
                   AND processing_status IN ('received','retry')
                   AND next_attempt_at <= now()
                   AND attempts < max_attempts
                 ORDER BY received_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
               )
               UPDATE provider_events event
               SET processing_status='running', locked_at=now(), locked_by=%s,
                   attempts=event.attempts+1, updated_at=now()
               FROM candidate
               WHERE event.id=candidate.id
               RETURNING event.*""",
            (worker_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def complete_novofon_event(
    event_id: str,
    call_id: int | None,
    employee_mapping_id: str | None,
    *,
    ignored: bool = False,
) -> None:
    """Mark an event terminally processed after its idempotent DB transaction."""

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE provider_events
               SET processing_status=%s, call_id=coalesce(%s, call_id),
                   employee_mapping_id=coalesce(%s, employee_mapping_id),
                   processing_error=NULL, processed_at=now(), locked_at=NULL,
                   locked_by=NULL, updated_at=now()
               WHERE id=%s""",
            ("ignored" if ignored else "processed", call_id, employee_mapping_id, event_id),
        )


def fail_novofon_event(event: dict, error: str) -> None:
    """Retry a provider event with bounded exponential backoff.

    The original event stays in the append-only ledger.  A malformed or
    repeatedly failing delivery is visible as ``failed`` rather than silently
    disappearing or poisoning the local ASR queue.
    """

    terminal = event["attempts"] >= event["max_attempts"]
    delay = min(3600, 2 ** min(int(event["attempts"]), 12))
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE provider_events
               SET processing_status=%s, processing_error=%s, locked_at=NULL,
                   locked_by=NULL,
                   next_attempt_at=CASE WHEN %s THEN next_attempt_at
                                        ELSE now()+(%s * interval '1 second') END,
                   updated_at=now()
               WHERE id=%s""",
            ("failed" if terminal else "retry", error[:4000], terminal, delay, event["id"]),
        )


def enqueue_reconciled_novofon_event(
    *,
    event_type: str,
    call_session_id: str,
    payload: dict,
    idempotency_key: str,
    occurred_at: datetime | None = None,
    recording_url: str | None = None,
) -> bool:
    """Durably enqueue a synthetic Data API event exactly once.

    Data API reconciliation is kept as a distinct immutable provider source.
    This lets a richer reconciliation row still update a call when a sparse
    webhook for the same session arrived first, while the canonical ``calls``
    row and missed-task creation remain idempotent.
    """

    link_hash = (
        hashlib.sha256(recording_url.encode("utf-8")).hexdigest()
        if recording_url
        else None
    )
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO provider_events(
                   source, provider, idempotency_key, event_type, call_session_id,
                   recording_url, recording_link_hash, occurred_at, payload,
                   signature_valid, provider_status, processing_status
               ) VALUES(
                   'novofon-data-api', 'novofon', %s, %s, %s, %s, %s, %s, %s::jsonb,
                   NULL, 'reconciled', 'received'
               )
               ON CONFLICT DO NOTHING""",
            (
                idempotency_key,
                event_type,
                call_session_id,
                recording_url,
                link_hash,
                occurred_at,
                Json(payload),
            ),
        )
        return cur.rowcount == 1


def _novofon_employee_mapping(cur, event: NovofonEvent) -> dict | None:
    """Resolve a provider employee to a CRM owner without creating a mapping."""

    cur.execute(
        """SELECT id, crm_user_id
           FROM provider_employee_mappings
           WHERE provider='novofon' AND active
             AND ((%s IS NOT NULL AND provider_employee_id=%s)
                  OR (%s IS NOT NULL AND provider_extension=%s))
           ORDER BY CASE WHEN provider_employee_id=%s THEN 0 ELSE 1 END,
                    updated_at DESC
           LIMIT 1""",
        (
            event.employee_id,
            event.employee_id,
            event.employee_extension,
            event.employee_extension,
            event.employee_id,
        ),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _novofon_initiation_mapping(cur, event: NovofonEvent) -> dict | None:
    """Fallback owner for a CRM-created callback with a sparse provider event."""

    if not event.call_session_id and not event.external_id:
        return None
    cur.execute(
        """SELECT pem.id, pem.crm_user_id
           FROM call_initiation_requests cir
           JOIN provider_employee_mappings pem ON pem.id=cir.employee_mapping_id
           WHERE cir.source='novofon' AND pem.active
             AND (cir.call_session_id=%s
                  OR (%s IS NOT NULL AND (cir.provider_request_id=%s OR cir.id::text=%s)))
           ORDER BY cir.requested_at DESC
           LIMIT 1""",
        (
            event.call_session_id,
            event.external_id,
            event.external_id,
            event.external_id,
        ),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _novofon_contact(cur, phone: str | None) -> str | None:
    if not phone:
        return None
    cur.execute(
        """INSERT INTO contacts(phone_normalized, phone_raw)
           VALUES(%s,%s)
           ON CONFLICT(phone_normalized) DO UPDATE
             SET phone_raw=coalesce(contacts.phone_raw, excluded.phone_raw),
                 updated_at=now()
           RETURNING id""",
        (phone, phone),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _novofon_call_state(event: NovofonEvent) -> tuple[str, str]:
    if event.event_type == "RECORD_CALL" and event.recording_url:
        # In the mobile pilot provider recording is the terminal automated
        # result. We must not leave every completed call in an eternal
        # "awaiting transcript" state when speech analytics is explicitly off.
        transcript_enabled = os.environ.get("NOVOFON_TRANSCRIPT_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        return ("awaiting_transcript", "new") if transcript_enabled else ("ready", "stored")
    if event.missed_inbound:
        return "ready", "stored"
    return "awaiting_recording", "new"


def _upsert_novofon_call(
    cur,
    event: NovofonEvent,
    payload: dict,
    contact_id: str | None,
    owner_id: str | None,
) -> int:
    """Create/update the canonical call row for a Novofon call session."""

    assert event.call_session_id
    processing_status, status = _novofon_call_state(event)
    caller_number = (
        event.contact_phone if event.direction == "in" else event.virtual_phone
    ) if event.direction_known else None
    callee_number = (
        event.virtual_phone if event.direction == "in" else event.contact_phone
    ) if event.direction_known else None
    authoritative_direction = event.event_type == "CALL_END" and event.direction_known
    authoritative_times = event.event_type == "CALL_END" and event.occurred_at is not None
    cur.execute(
        """INSERT INTO calls(
                 call_id, source, external_call_id, direction, caller_number,
                 callee_number, contact_id, owner_id, started_at, ended_at,
                 duration_sec, answered, processing_status, processing_error,
                 raw_json, status
               ) VALUES(
                 %s, 'novofon', %s, %s, %s, %s, %s, %s,
                 coalesce(%s, now()), coalesce(%s, now()), %s, %s, %s,
                 NULL, %s, %s
               )
               ON CONFLICT(source, external_call_id) DO UPDATE SET
                 direction=CASE WHEN %s THEN excluded.direction ELSE calls.direction END,
                 caller_number=CASE WHEN %s THEN coalesce(excluded.caller_number, calls.caller_number)
                                    ELSE calls.caller_number END,
                 callee_number=CASE WHEN %s THEN coalesce(excluded.callee_number, calls.callee_number)
                                    ELSE calls.callee_number END,
                 contact_id=coalesce(calls.contact_id, excluded.contact_id),
                 owner_id=coalesce(calls.owner_id, excluded.owner_id),
                 started_at=CASE WHEN %s THEN excluded.started_at ELSE calls.started_at END,
                 ended_at=CASE WHEN %s THEN excluded.ended_at ELSE calls.ended_at END,
                 duration_sec=greatest(coalesce(calls.duration_sec, 0),
                                     coalesce(excluded.duration_sec, 0)),
                 answered=calls.answered OR excluded.answered,
                 processing_status=CASE
                    WHEN calls.processing_status IN ('ready','transcribing','analyzing')
                      THEN calls.processing_status
                    WHEN excluded.processing_status='ready'
                      THEN excluded.processing_status
                    WHEN excluded.processing_status='awaiting_transcript'
                      THEN excluded.processing_status
                    WHEN calls.processing_status='received'
                      THEN excluded.processing_status
                    ELSE calls.processing_status
                 END,
                 status=CASE WHEN excluded.processing_status='ready' THEN 'stored'
                             ELSE calls.status END,
                 raw_json=excluded.raw_json,
                 updated_at=now()
               RETURNING id""",
        (
            f"novofon:{event.call_session_id}",
            event.call_session_id,
            event.direction,
            caller_number,
            callee_number,
            contact_id,
            owner_id,
            event.started_at,
            event.occurred_at,
            event.talk_duration_sec,
            event.answered,
            processing_status,
            Json(payload),
            status,
            authoritative_direction,
            authoritative_direction,
            authoritative_direction,
            authoritative_times,
            authoritative_times,
        ),
    )
    row = cur.fetchone()
    return int(row["id"])


def _attach_novofon_recording(cur, event: NovofonEvent, payload: dict, call_id: int, source_event_id: str) -> None:
    """Persist provider metadata only; pilot recordings are never downloaded."""

    if not event.recording_url:
        return
    recording_hash = event.recording_link_hash
    provider_recording_id = event.provider_recording_id
    if not provider_recording_id:
        return
    cur.execute(
        """INSERT INTO provider_recordings(
                 source, provider, provider_recording_id, provider_call_id,
                 call_session_id, recording_url, recording_link_hash, call_id,
                 source_event_id, channel, provider_status, duration_sec,
                 available_at, download_status, provider_metadata
               ) VALUES(
                 'novofon', 'novofon', %s, %s, %s, %s, %s, %s, %s, 'mixed',
                 'available', %s, coalesce(%s, now()), 'external', %s
               )
               ON CONFLICT(provider, provider_recording_id) DO UPDATE SET
                 provider_call_id=coalesce(provider_recordings.provider_call_id,
                                           excluded.provider_call_id),
                 call_session_id=coalesce(provider_recordings.call_session_id,
                                          excluded.call_session_id),
                 recording_url=coalesce(excluded.recording_url,
                                        provider_recordings.recording_url),
                 recording_link_hash=coalesce(excluded.recording_link_hash,
                                              provider_recordings.recording_link_hash),
                 call_id=coalesce(provider_recordings.call_id, excluded.call_id),
                 source_event_id=coalesce(provider_recordings.source_event_id,
                                          excluded.source_event_id),
                 duration_sec=greatest(coalesce(provider_recordings.duration_sec, 0),
                                      coalesce(excluded.duration_sec, 0)),
                 provider_status='available', download_status='external',
                 provider_metadata=excluded.provider_metadata, updated_at=now()""",
        (
            provider_recording_id,
            event.call_session_id,
            event.call_session_id,
            event.recording_url,
            recording_hash,
            call_id,
            source_event_id,
            event.recording_duration_sec or None,
            event.occurred_at,
            Json(payload),
        ),
    )


def _link_novofon_call_initiation(cur, event: NovofonEvent, call_id: int) -> None:
    """Connect callback intent to the provider's authoritative session id."""

    if not event.call_session_id:
        return
    cur.execute(
        """UPDATE call_initiation_requests
           SET call_session_id=coalesce(call_session_id, %s), call_id=coalesce(call_id, %s),
               status=CASE WHEN status IN ('requested','accepted','pending','in_progress')
                           THEN 'completed' ELSE status END,
               completed_at=CASE WHEN status IN ('requested','accepted','pending','in_progress')
                                 THEN now() ELSE completed_at END,
               updated_at=now()
           WHERE source='novofon'
             AND (call_session_id=%s
                  OR (%s IS NOT NULL AND (provider_request_id=%s OR id::text=%s OR idempotency_key=%s)))""",
        (
            event.call_session_id,
            call_id,
            event.call_session_id,
            event.external_id,
            event.external_id,
            event.external_id,
            event.external_id,
        ),
    )


def _create_missed_call_task(
    cur,
    event: NovofonEvent,
    call_id: int,
    contact_id: str | None,
    owner_id: str | None,
) -> None:
    """Create exactly one actionable callback task for a missed inbound call."""

    if not event.missed_inbound or not contact_id:
        return
    cur.execute(
        """INSERT INTO tasks(contact_id, call_id, assignee_id, title, description, due_at)
           SELECT %s, %s, %s, 'Перезвонить по пропущенному звонку', %s, now()
           WHERE NOT EXISTS (
             SELECT 1 FROM tasks
             WHERE call_id=%s AND status='open'
               AND title='Перезвонить по пропущенному звонку'
           )""",
        (
            contact_id,
            call_id,
            owner_id,
            f"Пропущенный входящий звонок Novofon от {event.contact_phone or 'неизвестного номера'}.",
            call_id,
        ),
    )


def process_novofon_event(event_row: dict) -> dict:
    """Apply a claimed Novofon event in one idempotent database transaction.

    It intentionally does *not* create local ASR/LLM jobs.  Novofon remains
    the recording system of record during the mobile-first pilot.
    """

    payload = event_row.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Novofon event payload is not a JSON object")
    event = parse_event(payload)
    if event.event_type not in {"CALL_END", "RECORD_CALL"}:
        return {"ignored": True, "call_id": None, "employee_mapping_id": None}
    if not event.call_session_id:
        raise ValueError(f"{event.event_type} event is missing call_session_id")
    configured_virtual_phone = normalize_phone(os.environ.get("NOVOFON_VIRTUAL_NUMBER", ""))
    if configured_virtual_phone and event.virtual_phone != configured_virtual_phone:
        # Do not rely solely on the nginx/API filter. A shared Novofon account
        # must never materialize another line's events after a configuration
        # error or direct replay into the provider-event ledger.
        return {"ignored": True, "call_id": None, "employee_mapping_id": None}

    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        mapping = _novofon_employee_mapping(cur, event) or _novofon_initiation_mapping(cur, event)
        mapping_id = str(mapping["id"]) if mapping else None
        owner_id = str(mapping["crm_user_id"]) if mapping else None
        contact_id = _novofon_contact(cur, event.contact_phone)
        call_id = _upsert_novofon_call(cur, event, payload, contact_id, owner_id)
        if event.event_type == "RECORD_CALL":
            _attach_novofon_recording(cur, event, payload, call_id, str(event_row["id"]))
        if event.event_type == "CALL_END":
            _create_missed_call_task(cur, event, call_id, contact_id, owner_id)
        _link_novofon_call_initiation(cur, event, call_id)
        return {"ignored": False, "call_id": call_id, "employee_mapping_id": mapping_id}
