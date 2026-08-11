import json
import logging
import os
import socket
import time
from pathlib import Path

from asr import build_transcript
from db import (call_context, claim_job, claim_novofon_event, complete_job,
                complete_novofon_event, fail_job, fail_novofon_event, ingest,
                latest_transcript, process_novofon_event,
                recover_stale_jobs, recover_stale_novofon_events, save_insight,
                save_transcript)
from llm import MODEL_NAME, PROMPT_VERSION, summarize
from reconcile import NovofonReconciler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mo54-worker")
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "/queue"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
ASR_MODEL = "gigaam-v3-e2e-rnnt"
PROVIDER_EVENT_LEASE_MINUTES = int(os.environ.get("PROVIDER_EVENT_LEASE_MINUTES", "15"))


def ingest_files() -> None:
    """Asterisk writes atomically; after this bridge PostgreSQL owns all job state."""
    for path in sorted(QUEUE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            call_id = ingest(job)
            path.replace(path.with_suffix(".done" if call_id else ".ignored"))
        except Exception:
            log.exception("Failed to ingest %s; it remains for retry", path.name)


def process(job: dict) -> None:
    started = time.monotonic()
    context = call_context(job["call_id"])
    try:
        if job["kind"] == "transcribe":
            text, segments = build_transcript(context["customer_path"], context["manager_path"])
            if not segments:
                raise ValueError("Both recording channels are empty or unavailable")
            save_transcript(job["call_id"], text, segments, ASR_MODEL)
        elif job["kind"] == "analyze":
            transcript = latest_transcript(job["call_id"])
            if not transcript:
                raise ValueError("Transcript is missing")
            insight = summarize(transcript["text"])
            save_insight(job["call_id"], insight, MODEL_NAME, PROMPT_VERSION)
        else:
            raise ValueError(f"Unsupported job kind: {job['kind']}")
        complete_job(job["id"], int((time.monotonic() - started) * 1000))
    except Exception as exc:
        fail_job(job, f"{type(exc).__name__}: {exc}")
        raise


def process_provider_event(event: dict) -> None:
    """Process a durable Novofon notification before local ASR/LLM jobs.

    The mobile-first pilot deliberately stops at provider recording metadata;
    `process_novofon_event` never queues a transcription or LLM job.
    """

    try:
        result = process_novofon_event(event)
        complete_novofon_event(
            str(event["id"]),
            result.get("call_id"),
            result.get("employee_mapping_id"),
            ignored=bool(result.get("ignored")),
        )
        state = "ignored" if result.get("ignored") else "processed"
        log.info("Novofon event %s %s", event["id"], state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        fail_novofon_event(event, error)
        raise


def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    recovered = recover_stale_jobs()
    recovered_provider = recover_stale_novofon_events(PROVIDER_EVENT_LEASE_MINUTES)
    log.info(
        "Worker %s started; recovered %d stale local jobs and %d provider events",
        WORKER_ID,
        recovered,
        recovered_provider,
    )
    reconciler = NovofonReconciler()
    if reconciler.enabled:
        if reconciler.configured:
            log.info("Novofon Data API reconciliation is enabled")
        else:
            log.warning("Novofon Data API reconciliation is enabled but not fully configured")
    while True:
        # This only appends events to the provider ledger.  They are then
        # claimed below by the same idempotent event-processing path as a
        # signed webhook delivery.
        reconciler.maybe_reconcile()
        ingest_files()
        provider_event = claim_novofon_event(WORKER_ID)
        if provider_event:
            try:
                process_provider_event(provider_event)
            except Exception:
                log.exception("Novofon event %s failed", provider_event["id"])
            continue
        job = claim_job(WORKER_ID)
        if job:
            try:
                process(job)
            except Exception:
                log.exception("Job %s failed", job["id"])
            continue
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
