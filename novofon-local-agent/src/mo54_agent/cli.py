from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .asr import LocalASR, Transcript
from .browser import NovofonBrowser
from .config import AgentConfig, AgentPaths
from .crm import CRMClient
from .errors import AgentError
from .review import load_approved_review, preserve_automatic_baseline, review_pilot
from .runner import AgentRunner
from .security import set_crm_token
from .store import AgentLock, AgentStore


def _apply_manual_pilot_roles(
    transcript_path: Path,
    *,
    manager_labels: set[str],
    customer_labels: set[str],
) -> dict[str, int]:
    """Apply an operator-confirmed role map without printing transcript text."""
    if not manager_labels and not customer_labels:
        raise AgentError("manual_roles_missing", "Choose at least one local speaker label", retryable=False)
    overlap = manager_labels & customer_labels
    if overlap:
        raise AgentError("manual_roles_conflict", "One speaker cannot be both manager and customer", retryable=False)

    # Keep raw ASR output separately before an operator annotation changes the
    # working transcript.  The review artifact later hashes that source.
    preserve_automatic_baseline(transcript_path)
    raw = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = raw.get("segments")
    if not isinstance(segments, list) or not segments:
        raise AgentError("pilot_transcript_invalid", "The local pilot transcript has no segments", retryable=False)
    labels = {
        str(segment.get("speaker_label") or "").strip()
        for segment in segments
        if str(segment.get("speaker_label") or "").strip()
    }
    requested = manager_labels | customer_labels
    if not requested <= labels:
        raise AgentError("manual_role_label_unknown", "A selected speaker label is not present in the local transcript", retryable=False)

    counts = {"manager": 0, "customer": 0}
    for segment in segments:
        label = str(segment.get("speaker_label") or "").strip()
        if label in manager_labels:
            segment["role"] = "manager"
            counts["manager"] += 1
        elif label in customer_labels:
            segment["role"] = "customer"
            counts["customer"] += 1

    # The audit note stays in the local JSON. The text-only CRM contract sees
    # only the explicit segment roles after operator confirmation.
    raw["manual_role_assignment"] = {
        "method": "operator_confirmation",
        "assigned_at": datetime.now(timezone.utc).isoformat(),
        "manager_labels": sorted(manager_labels),
        "customer_labels": sorted(customer_labels),
    }
    temporary = transcript_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    temporary.replace(transcript_path)
    return counts


def _components() -> tuple[AgentPaths, AgentConfig, AgentStore]:
    paths = AgentPaths.default()
    paths.ensure()
    config = AgentConfig.load(paths)
    store = AgentStore(paths.database)
    store.initialize()
    return paths, config, store


def _install_task(paths: AgentPaths) -> None:
    if sys.platform != "win32":
        raise AgentError("windows_required", "Windows Task Scheduler is required", retryable=False)
    executable = Path(sys.executable).with_name("mo54-agent.exe")
    command = [
        "schtasks.exe", "/Create", "/TN", "MO54CallsAgent", "/SC", "MINUTE", "/MO", "30", "/TR",
        f'"{executable}" run-once', "/RL", "LIMITED", "/IT", "/F",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AgentError("scheduled_task_failed", "Could not create Windows scheduled task", retryable=False)
    # schtasks cannot consistently set WakeToRun; setup.ps1 uses the full ScheduledTasks API.


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mo54-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preflight = subcommands.add_parser("preflight")
    preflight.add_argument("--prepare-models", action="store_true")
    provision = subcommands.add_parser("provision-browser")
    provision.add_argument("--wait-seconds", type=int, default=600)
    enroll = subcommands.add_parser("enroll-manager")
    enroll.add_argument("reference_audio", type=Path)
    subcommands.add_parser("inventory")
    subcommands.add_parser("inspect-layout")
    subcommands.add_parser("run-once")
    # Intentionally local-only: useful for accepting a pilot recording before
    # CRM credentials and the server deployment are in place.
    download_pilot = subcommands.add_parser("download-pilot")
    download_pilot.add_argument("call_session_id")
    transcribe_pilot = subcommands.add_parser("transcribe-pilot")
    transcribe_pilot.add_argument("call_session_id")
    assign_roles = subcommands.add_parser("assign-pilot-roles")
    assign_roles.add_argument("call_session_id")
    assign_roles.add_argument("--manager-label", action="append", default=[])
    assign_roles.add_argument("--customer-label", action="append", default=[])
    review_pilot_command = subcommands.add_parser("review-pilot")
    review_pilot_command.add_argument("call_session_id")
    deliver_pilot = subcommands.add_parser("deliver-pilot")
    deliver_pilot.add_argument("call_session_id")
    subcommands.add_parser("exclude-pending-downloads")
    subcommands.add_parser("status")
    subcommands.add_parser("install-task")
    subcommands.add_parser("set-crm-token")
    args = parser.parse_args(argv)
    try:
        paths, config, store = _components()
        if args.command == "preflight":
            asr = LocalASR(paths, config)
            if args.prepare_models:
                token = getpass.getpass("Temporary Hugging Face token (not saved): ")
                asr.prepare_models(token)
            checks = asr.verify_ready()
            checks += [] if shutil.which("msedge") or Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe").exists() else ["edge_missing"]
            if not config.novofon_calls_url:
                checks.append("novofon_url_missing")
            print(json.dumps({"ready": not checks, "problems": checks}, ensure_ascii=False))
            if checks:
                raise SystemExit(2)
        elif args.command == "provision-browser":
            NovofonBrowser(paths, config).provision(args.wait_seconds)
        elif args.command == "enroll-manager":
            LocalASR(paths, config).enroll_manager(args.reference_audio)
        elif args.command == "inventory":
            with AgentLock(paths.lock):
                since = datetime.now(timezone.utc) - timedelta(days=7)
                visible = new = 0
                for item in NovofonBrowser(paths, config).inventory():
                    visible += 1
                    if store.record_inventory(item.call_session_id, item.started_at, item.duration_sec, since):
                        new += 1
                print(json.dumps({"visible_calls": visible, "new_calls": new}, ensure_ascii=False))
        elif args.command == "inspect-layout":
            with AgentLock(paths.lock):
                print(json.dumps(NovofonBrowser(paths, config).inspect_layout(), ensure_ascii=False))
        elif args.command == "run-once":
            with AgentLock(paths.lock):
                print(json.dumps(AgentRunner(paths, config, store).run_once(), ensure_ascii=False))
        elif args.command == "download-pilot":
            with AgentLock(paths.lock):
                runner = AgentRunner(paths, config, store)
                audio = runner.download_pilot(args.call_session_id)
                record = store.get(args.call_session_id)
                inventory_duration = record.duration_sec if record else None
                # This local-only receipt provides the acceptance metadata
                # without printing an audio path, browser data, or transcript.
                print(json.dumps({
                    "downloaded": 1,
                    "call_session_id": args.call_session_id,
                    "audio_sha256": audio.sha256,
                    "audio_duration_sec": audio.duration_sec,
                    "inventory_duration_sec": inventory_duration,
                    "duration_delta_sec": abs(inventory_duration - audio.duration_sec) if inventory_duration is not None else None,
                    "duration_within_tolerance": runner.duration_within_inventory_tolerance(inventory_duration, audio.duration_sec),
                }, ensure_ascii=False))
        elif args.command == "transcribe-pilot":
            with AgentLock(paths.lock):
                record = store.get(args.call_session_id)
                if not record or record.status not in {"downloaded", "asr_retry"}:
                    raise AgentError("pilot_call_not_transcribable", "The selected pilot call is not ready for local ASR", retryable=False)
                if not record.audio_path or not record.audio_path.is_file():
                    raise AgentError("pilot_audio_missing", "The downloaded pilot audio is unavailable", retryable=False)
                if not store.claim_transcription(record.call_session_id):
                    raise AgentError("state_conflict", "The pilot recording is already being processed", retryable=True)
                runner = AgentRunner(paths, config, store)
                started = time.monotonic()
                try:
                    transcript = runner._transcribe(record.call_session_id, record.audio_path)
                    transcript_path = runner._save_transcript(record.call_session_id, transcript)
                    store.mark_transcribed(record.call_session_id, transcript_path, transcript.asr_model, transcript.language)
                    roles = {role: sum(segment.role == role for segment in transcript.segments) for role in ("manager", "customer", "unknown")}
                    print(json.dumps({
                        "transcribed": 1,
                        "call_session_id": args.call_session_id,
                        "audio_sha256": record.audio_sha256,
                        "audio_duration_sec": record.audio_duration_sec,
                        "inventory_duration_sec": record.duration_sec,
                        "duration_delta_sec": abs(record.duration_sec - record.audio_duration_sec) if record.duration_sec is not None and record.audio_duration_sec is not None else None,
                        "duration_within_tolerance": runner.duration_within_inventory_tolerance(record.duration_sec, record.audio_duration_sec),
                        "segments": len(transcript.segments),
                        "roles": roles,
                        "language": transcript.language,
                        "processing_seconds": round(time.monotonic() - started, 2),
                        "review_required_before_delivery": True,
                    }, ensure_ascii=False))
                except AgentError as exc:
                    store.mark_error(record.call_session_id, exc.code, asr=True)
                    raise
        elif args.command == "assign-pilot-roles":
            with AgentLock(paths.lock):
                record = store.get(args.call_session_id)
                if not record or record.status != "transcribed" or not record.transcript_path:
                    raise AgentError("pilot_call_not_reviewable", "The selected pilot call is not ready for local role review", retryable=False)
                counts = _apply_manual_pilot_roles(
                    record.transcript_path,
                    manager_labels={label.strip() for label in args.manager_label if label.strip()},
                    customer_labels={label.strip() for label in args.customer_label if label.strip()},
                )
                print(json.dumps({
                    "call_session_id": args.call_session_id,
                    "manual_roles_applied": True,
                    "assigned_segments": counts,
                    "crm_contacted": False,
                }, ensure_ascii=False))
        elif args.command == "review-pilot":
            with AgentLock(paths.lock):
                record = store.get(args.call_session_id)
                if not record or record.status != "transcribed":
                    raise AgentError("pilot_call_not_reviewable", "The selected pilot call is not ready for local review", retryable=False)
                artifact = review_pilot(
                    paths,
                    record,
                    on_started=lambda url: print(json.dumps({
                        "call_session_id": args.call_session_id,
                        "review_url": url,
                        "bound_to_loopback": True,
                        "crm_contacted": False,
                    }, ensure_ascii=False), flush=True),
                )
                print(json.dumps({
                    "call_session_id": args.call_session_id,
                    "review_approved": True,
                    "review_artifact": artifact.name,
                    "crm_contacted": False,
                }, ensure_ascii=False))
        elif args.command == "deliver-pilot":
            with AgentLock(paths.lock):
                record = store.get(args.call_session_id)
                if not record:
                    raise AgentError("pilot_call_not_deliverable", "The selected pilot call is not ready for CRM delivery", retryable=False)
                if record.status == "sent":
                    # The first confirmed delivery makes this approved review
                    # immutable.  A local rerun therefore cannot create a
                    # hidden second transcript version.
                    load_approved_review(paths, record)
                    print(json.dumps({"delivered": 0, "already_delivered": True}, ensure_ascii=False))
                    return
                if record.status != "transcribed":
                    raise AgentError("pilot_call_not_deliverable", "The selected pilot call is not ready for CRM delivery", retryable=False)
                if not record.audio_sha256 or not record.audio_duration_sec or not record.transcript_path:
                    raise AgentError("pilot_transcript_missing", "The pilot transcript metadata is incomplete", retryable=False)
                if not AgentRunner.duration_within_inventory_tolerance(record.duration_sec, record.audio_duration_sec):
                    raise AgentError("pilot_duration_mismatch", "Downloaded audio duration needs manual review before CRM delivery", retryable=False)
                transcript = load_approved_review(paths, record)
                # CRMClient receives the metadata object only.  Its path is never
                # opened or uploaded; the endpoint contract contains text only.
                from .browser import DownloadedAudio
                response = CRMClient(config).send_transcript(
                    record.call_session_id,
                    DownloadedAudio(record.audio_path or paths.audio / "local-only", record.audio_sha256, record.audio_duration_sec),
                    transcript,
                )
                store.mark_sent(record.call_session_id)
                print(json.dumps({"delivered": 1, "crm_idempotent": bool(response.get("idempotent"))}, ensure_ascii=False))
        elif args.command == "exclude-pending-downloads":
            with AgentLock(paths.lock):
                print(json.dumps({"excluded": store.block_pending_downloads("excluded_before_schedule")}, ensure_ascii=False))
        elif args.command == "status":
            print(json.dumps({"calls": store.summary(), "root": str(paths.root)}, ensure_ascii=False))
        elif args.command == "install-task":
            _install_task(paths)
        elif args.command == "set-crm-token":
            set_crm_token(getpass.getpass("CRM local-agent token: "))
    except AgentError as exc:
        print(json.dumps({"error": exc.code}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
