import json
import math
import os
import re
import time
import uuid

import certifi
import httpx
from jsonschema import Draft202012Validator

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gigachat")
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY", "")
GIGACHAT_MODEL = os.environ.get("GIGACHAT_MODEL", "GigaChat")
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _gigachat_ca_bundle() -> str:
    """Return a CA bundle without ever disabling TLS certificate verification.

    The worker image installs the official Russian Trusted Root and Sub CA in
    Debian's system trust store.  An explicit environment override remains
    available for an organisation-managed bundle.
    """
    configured_bundle = os.environ.get("GIGACHAT_CA_BUNDLE")
    if configured_bundle:
        if not os.path.isfile(configured_bundle):
            raise RuntimeError(f"GIGACHAT_CA_BUNDLE does not exist: {configured_bundle}")
        return configured_bundle
    if os.path.isfile(SYSTEM_CA_BUNDLE):
        return SYSTEM_CA_BUNDLE
    return certifi.where()


GIGACHAT_CA_BUNDLE = _gigachat_ca_bundle()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")
PROMPT_VERSION = "sales-v1.7-legacy-function-transport"
MODEL_NAME = {"gigachat": GIGACHAT_MODEL, "openai": OPENAI_MODEL, "ollama": OLLAMA_MODEL}.get(LLM_PROVIDER, LLM_PROVIDER)
_token, _token_expiry = "", 0.0

COMMERCIAL_FIELD_VALUE_SCHEMAS = {
    "qualification_segment": {"enum": ["under_80k", "over_80k", "unknown", None]},
    "estimated_budget_min": {"type": ["number", "null"], "minimum": 0},
    "estimated_budget_max": {"type": ["number", "null"], "minimum": 0},
    "budget_range": {"enum": ["under_80", "80_120", "120_160", "160_200", "over_200", "unknown", None]},
    "pain_primary": {"type": ["string", "null"]},
    "pain_secondary": {"type": "array", "items": {"type": "string"}},
    "customer_quote": {"type": ["string", "null"]},
    "decision_makers": {"type": "array", "items": {"type": "string"}},
    "decision_maker_status": {"enum": ["single", "multiple", "other_person_required", "unknown", None]},
    "alternative_considered": {"type": ["string", "null"]},
    "alternative_reason": {"type": ["string", "null"]},
    "desired_install_date": {"type": ["string", "null"], "format": "date"},
    "desired_install_period": {"type": ["string", "null"]},
    "next_contact_at": {"type": ["string", "null"], "format": "date-time"},
    "suggested_stage": {"enum": [
        "new_lead", "contacted", "qualified", "measure_scheduled", "measure_completed",
        "proposal_sent", "decision_pending", "contract_signed", "prepayment_received",
        "production", "installation_scheduled", "installed", "closed_won", "closed_lost",
        "disqualified", None,
    ]},
}


def _unknown_commercial_field(value_schema: dict) -> dict:
    """Return a schema-valid, deliberately non-actionable field proposal."""

    # Arrays must stay arrays to satisfy the schema.  Every other commercial
    # value schema accepts null, which means "the transcript does not support
    # a value" without inventing one.
    value = [] if value_schema.get("type") == "array" else None
    return {
        "proposed_value": value,
        "confidence": None,
        "evidence": [],
        "inference_status": "unknown",
    }


def _unknown_commercial_fields() -> dict:
    return {
        name: _unknown_commercial_field(value_schema)
        for name, value_schema in COMMERCIAL_FIELD_VALUE_SCHEMAS.items()
    }


def _field_proposal_schema(value_schema: dict) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["proposed_value", "confidence", "evidence", "inference_status"],
        "properties": {
            "proposed_value": value_schema,
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "evidence": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["segment_ordinal", "segment_start_ms", "segment_end_ms", "quote"],
                "properties": {
                    "segment_ordinal": {"type": "integer", "minimum": 0},
                    "segment_start_ms": {"type": ["integer", "null"], "minimum": 0},
                    "segment_end_ms": {"type": ["integer", "null"], "minimum": 0},
                    "quote": {"type": "string", "minLength": 1, "maxLength": 400},
                },
            }},
            "inference_status": {"enum": ["supported", "inferred", "unknown"]},
        },
    }

INSIGHT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "customer_intent", "customer_need", "product", "budget_amount",
        "timeline", "decision_maker", "lead_stage", "lead_temperature", "objections",
        "manager_responses", "agreements", "customer_promises", "company_promises",
        "next_step", "next_step_date", "next_step_owner", "loss_risk", "outcome",
        "quality_scores", "recommendations", "evidence", "confidence", "commercial_proposal",
    ],
    "properties": {
        "summary": {"type": ["string", "null"]},
        "customer_intent": {"type": ["string", "null"]},
        "customer_need": {"type": ["string", "null"]},
        "product": {"type": ["string", "null"]},
        "budget_amount": {"type": ["number", "null"], "minimum": 0},
        "timeline": {"type": ["string", "null"]},
        "decision_maker": {"type": ["string", "null"]},
        "lead_stage": {"enum": ["new", "qualified", "proposal", "negotiation", "won", "lost", None]},
        "lead_temperature": {"enum": ["cold", "warm", "hot", None]},
        "objections": {"type": "array", "items": {"type": "string"}},
        "manager_responses": {"type": "array", "items": {"type": "string"}},
        "agreements": {"type": "array", "items": {"type": "string"}},
        "customer_promises": {"type": "array", "items": {"type": "string"}},
        "company_promises": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": ["string", "null"]},
        "next_step_date": {"type": ["string", "null"], "format": "date"},
        "next_step_owner": {"enum": ["manager", "customer", "company", None]},
        "loss_risk": {"enum": ["low", "medium", "high", None]},
        "outcome": {"enum": ["new_lead", "continue_work", "proposal_needed", "measurement_needed",
                             "prepayment_or_contract", "no_answer", "lost", "no_action_required", None]},
        "quality_scores": {
            "type": "object", "additionalProperties": False,
            "required": ["discovery", "clarity", "objection_handling", "next_step"],
            "properties": {key: {"type": ["integer", "null"], "minimum": 0, "maximum": 5}
                           for key in ("discovery", "clarity", "objection_handling", "next_step")},
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["field", "segment_ordinal", "segment_start_ms", "segment_end_ms", "quote"],
                "properties": {
                    "field": {"type": "string"},
                    "segment_ordinal": {"type": "integer", "minimum": 0},
                    "segment_start_ms": {"type": ["integer", "null"], "minimum": 0},
                    "segment_end_ms": {"type": ["integer", "null"], "minimum": 0},
                    "quote": {"type": "string", "minLength": 1, "maxLength": 400},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "commercial_proposal": {"type": "object", "additionalProperties": False,
            "required": ["fields"],
            "properties": {"fields": {"type": "object", "additionalProperties": False,
                "required": list(COMMERCIAL_FIELD_VALUE_SCHEMAS),
                "properties": {name: _field_proposal_schema(schema)
                               for name, schema in COMMERCIAL_FIELD_VALUE_SCHEMAS.items()}}}},
    },
}


# The current GigaChat legacy function endpoint accepts a deliberately small
# JSON Schema dialect.  This is a transport contract, not the persistence
# contract: every response is expanded and then checked against the complete
# ``INSIGHT_SCHEMA`` locally before it can be stored.
GIGACHAT_TRANSPORT_STRING_FIELDS = (
    "summary",
    "customer_intent",
    "customer_need",
    "product",
    "timeline",
    "decision_maker",
    "lead_stage",
    "lead_temperature",
    "next_step",
    "next_step_owner",
    "loss_risk",
    "outcome",
)
GIGACHAT_TRANSPORT_ARRAY_FIELDS = (
    "objections",
    "manager_responses",
    "agreements",
    "customer_promises",
    "company_promises",
    "recommendations",
)
GIGACHAT_TRANSPORT_EVIDENCE_FIELDS = ("field", "segment_ordinal", "quote")
GIGACHAT_TRANSPORT_EVIDENCE_FIELD_SET = frozenset(GIGACHAT_TRANSPORT_EVIDENCE_FIELDS)
GIGACHAT_TRANSPORT_REQUIRED_FIELDS = (
    *GIGACHAT_TRANSPORT_STRING_FIELDS,
    *GIGACHAT_TRANSPORT_ARRAY_FIELDS,
    "confidence",
    "evidence",
    "commercial_proposal",
)
GIGACHAT_TRANSPORT_REQUIRED_FIELD_SET = frozenset(GIGACHAT_TRANSPORT_REQUIRED_FIELDS)
GIGACHAT_LEGACY_INSIGHT_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        **{name: {"type": "string"} for name in GIGACHAT_TRANSPORT_STRING_FIELDS},
        **{
            name: {"type": "array", "items": {"type": "string"}}
            for name in GIGACHAT_TRANSPORT_ARRAY_FIELDS
        },
        "confidence": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "segment_ordinal": {"type": "integer"},
                    "quote": {"type": "string"},
                },
            },
        },
        # Empty properties are intentional: `{}` is a valid no-proposal
        # result. The local commercial fallback expands it to safe unknowns.
        "commercial_proposal": {"type": "object", "properties": {}},
    },
    "required": list(GIGACHAT_TRANSPORT_REQUIRED_FIELDS),
}

COMMERCIAL_PROPOSAL_SCHEMA = INSIGHT_SCHEMA["properties"]["commercial_proposal"]
validator = Draft202012Validator(INSIGHT_SCHEMA)
commercial_proposal_validator = Draft202012Validator(COMMERCIAL_PROPOSAL_SCHEMA)

SYSTEM_PROMPT = f"""Ты — аналитик продаж небольшой компании. Анализируй только факты из транскрипта.
Не выдумывай сведения: неизвестные скалярные значения указывай как null, списки — как [].
Верни только JSON, соответствующий этой JSON Schema:
{json.dumps(INSIGHT_SCHEMA, ensure_ascii=False)}
Оценки quality_scores: 0 — навык отсутствует, 5 — выполнен отлично. Каждая важная
квалификация должна иметь короткую подтверждающую цитату в evidence. Рекомендации
должны быть конкретными и относиться к следующему разговору."""


EVIDENCE_REQUIREMENTS = """
Сообщение пользователя — JSON с каноническим транскриптом и упорядоченным
таймлайном `segments`. У каждой строки есть `role`, `started_ms`, `ended_ms` и
`text`. Считай этот таймлайн единственным источником доказательств.

Для каждого элемента верхнего массива `evidence` и каждого непустого массива
`commercial_proposal.fields.*.evidence` значения `segment_start_ms` и
`segment_end_ms` должны в точности совпадать с одной исходной строкой
таймлайна. `quote` должна быть непустой дословной подстрокой поля `text` этой
же строки: не перефразируй, не нормализуй, не объединяй строки и не ссылайся
на соседний интервал. Если точную цитату дать нельзя, оставь соответствующее
коммерческое поле неизвестным с пустым массивом evidence. Evidence не даёт
права автоматически менять сделку, контакт или задачу: это только черновик
для ручной проверки.
""".strip()

# Keep this compact English addendum alongside the Russian instruction above:
# it is unambiguous for all supported providers and states the exact identity
# contract enforced below.  The user payload deliberately contains no flattened
# transcript, only the stored timeline rows.
TIMELINE_ONLY_REQUIREMENTS = """
The user payload contains only the persisted, ordered `segments` timeline.
Treat no other text as input. Every evidence item, including every
`commercial_proposal.fields.*.evidence` item, must contain `segment_ordinal`,
`segment_start_ms`, `segment_end_ms`, and `quote`. All four values must identify
one and only one input segment: ordinal and both timestamps must be exact, and
quote must be an exact, case-sensitive substring of that same segment's text.
Some manually reviewed segments intentionally have no audio timecode and use
`started_ms: null` together with `ended_ms: null`. When citing such a segment,
copy that exact null pair. Never use only one null, invent a timestamp, combine
neighbouring segments, or cite a flattened transcript.
""".strip()
COMMERCIAL_PROPOSAL_COMPLETENESS_REQUIREMENTS = f"""
Always include `commercial_proposal` with a `fields` object containing every
one of these keys: {", ".join(COMMERCIAL_FIELD_VALUE_SCHEMAS)}. Do not omit an
unknown field. For an unknown scalar use exactly `proposed_value: null`,
`confidence: null`, `evidence: []`, and `inference_status: "unknown"`. For the
array fields `pain_secondary` and `decision_makers`, use `proposed_value: []`
instead. An unknown field must not contain evidence.
""".strip()
SYSTEM_PROMPT = (
    f"{SYSTEM_PROMPT}\n\n{EVIDENCE_REQUIREMENTS}\n\n{TIMELINE_ONLY_REQUIREMENTS}"
    f"\n\n{COMMERCIAL_PROPOSAL_COMPLETENESS_REQUIREMENTS}"
)
GIGACHAT_INSIGHT_FUNCTION_NAME = "submit_sales_insight"
GIGACHAT_FUNCTION_COMMERCIAL_REQUIREMENTS = """
For `commercial_proposal`, return `{}` unless you can supply a complete,
evidence-backed fields object. Never emit a partial or uncertain commercial
proposal: it must not create a reviewable business draft.
""".strip()
GIGACHAT_FUNCTION_SYSTEM_PROMPT = f"""You are a sales-call analyst. Treat the user JSON `segments` as the only source of facts.
Return the result exclusively by calling `{GIGACHAT_INSIGHT_FUNCTION_NAME}`; do not write an
answer in `content`. Supply every required transport key and add no extra keys.

In this function contract, an exactly empty string means unknown for every string field. Do not
use null for those fields and do not use numeric 0 to mean unknown. Use [] for an unknown list.
`confidence` must be a real number from 0 to 1.

Each `evidence` item must contain only `field`, `segment_ordinal`, and `quote`. The ordinal must
identify one input segment exactly, and quote must be an exact case-sensitive substring of that
same segment's text. Do not send timestamps: the server copies the stored timecode pair itself.

{GIGACHAT_FUNCTION_COMMERCIAL_REQUIREMENTS}"""


class LLMError(RuntimeError):
    pass


def _timeline_segments(segments: list[dict]) -> list[dict]:
    """Return a bounded, role-aware view of the stored transcript rows."""

    if not isinstance(segments, list):
        raise LLMError("Transcript segments must be a list")
    timeline: list[dict] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise LLMError("Transcript segment must be an object")
        text = segment.get("text")
        if not isinstance(text, str) or not text:
            raise LLMError("Transcript segment text is missing")
        ordinal = segment.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise LLMError("Transcript segment ordinal is missing or invalid")
        role = segment.get("speaker") or segment.get("role") or "unknown"
        if role not in {"customer", "manager", "unknown"}:
            role = "unknown"
        if "started_ms" not in segment or "ended_ms" not in segment:
            raise LLMError("Transcript segment timecode pair is missing")
        started = segment["started_ms"]
        ended = segment["ended_ms"]
        if started is None or ended is None:
            if started is not None or ended is not None:
                raise LLMError("Transcript segment timecode must be a pair of nulls or integers")
        elif (
            not isinstance(started, int)
            or isinstance(started, bool)
            or not isinstance(ended, int)
            or isinstance(ended, bool)
            or started < 0
            or ended <= started
        ):
            raise LLMError("Transcript segment timecode is invalid")
        timeline.append(
            {
                "ordinal": ordinal,
                "role": role,
                "started_ms": started,
                "ended_ms": ended,
                "text": text,
            }
        )
    return timeline


def build_analysis_input(segments: list[dict]) -> str:
    """Build the sole LLM payload from persisted timeline rows only.

    ``transcripts.text`` is a convenience projection and can diverge from the
    row-level timeline after review.  It is therefore intentionally absent: a
    model may reason only over the immutable transcript_segments supplied by
    ``latest_transcript``.
    """

    return json.dumps(
        {"segments": _timeline_segments(segments)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_commercial_proposal(data: dict) -> None:
    """Reject invented commercial facts even if their JSON types look valid."""

    fields = data["commercial_proposal"]["fields"]
    for name, proposal in fields.items():
        value = proposal["proposed_value"]
        status = proposal["inference_status"]
        evidence = proposal["evidence"]
        unknown_value = value is None or value == [] or value == "unknown"
        if status == "unknown":
            if not unknown_value:
                raise LLMError(f"{name}: unknown field must not contain a proposed value")
            if evidence:
                raise LLMError(f"{name}: unknown field must not contain evidence")
            continue
        if unknown_value:
            raise LLMError(f"{name}: supported/inferred field needs a value")
        if proposal["confidence"] is None:
            raise LLMError(f"{name}: supported/inferred field needs confidence")
        if not evidence:
            raise LLMError(f"{name}: supported/inferred field needs evidence")


def _validate_evidence_references(data: dict, segments: list[dict]) -> None:
    """Require every model citation to point to one exact persisted segment."""

    source_by_identity: dict[tuple[int, int | None, int | None], str] = {}
    for segment in _timeline_segments(segments):
        ordinal = segment["ordinal"]
        started = segment["started_ms"]
        ended = segment["ended_ms"]
        identity = (ordinal, started, ended)
        if identity in source_by_identity:
            # This must be impossible for persisted rows because the database
            # has UNIQUE(transcript_id, ordinal).  Treat a malformed caller as
            # untrusted instead of accepting an ambiguous evidence reference.
            raise LLMError("Transcript segment identity is not unique")
        source_by_identity[identity] = segment["text"]

    def validate(items: list[dict], context: str) -> None:
        for item in items:
            started = item["segment_start_ms"]
            ended = item["segment_end_ms"]
            if started is None or ended is None:
                if started is not None or ended is not None:
                    raise LLMError(f"{context}: evidence timecode must be a pair of nulls or integers")
            elif (
                not isinstance(started, int)
                or isinstance(started, bool)
                or not isinstance(ended, int)
                or isinstance(ended, bool)
                or started < 0
                or ended <= started
            ):
                raise LLMError(f"{context}: evidence timecode is invalid")
            identity = (
                item["segment_ordinal"],
                started,
                ended,
            )
            source_text = source_by_identity.get(identity)
            if source_text is None:
                raise LLMError(f"{context}: evidence identity does not match a transcript segment")
            quote = item["quote"]
            if quote not in source_text:
                raise LLMError(f"{context}: evidence quote is not an exact transcript substring")

    validate(data["evidence"], "evidence")
    for name, proposal in data["commercial_proposal"]["fields"].items():
        validate(proposal["evidence"], name)


def _is_complete_commercial_proposal(proposal: object) -> bool:
    """Return whether a proposal is safe to retain exactly as supplied.

    This intentionally checks both JSON shape and the additional guardrails
    that make a commercial value actionable.  Evidence identity is validated
    separately against the stored transcript after the top-level insight
    schema passes; it is never silently repaired here.
    """

    if list(commercial_proposal_validator.iter_errors(proposal)):
        return False
    try:
        _validate_commercial_proposal({"commercial_proposal": proposal})
    except (LLMError, KeyError, TypeError):
        return False
    return True


def _normalize_commercial_proposal(data: object) -> None:
    """Fail closed when a provider returns an incomplete proposal object.

    A malformed commercial proposal can contain a mix of valid-looking and
    incomplete field values.  Retaining any subset would make unsupported
    business changes look reviewable, so *any* missing, empty, partial, or
    malformed proposal is replaced wholesale with explicit ``unknown``
    fields.  The rest of the top-level insight is untouched: its schema and
    all evidence references are still validated strictly below.
    """

    if not isinstance(data, dict):
        return
    if _is_complete_commercial_proposal(data.get("commercial_proposal")):
        return
    data["commercial_proposal"] = {"fields": _unknown_commercial_fields()}


def _parse(raw: str, *, segments: list[dict] | None = None) -> dict:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Invalid JSON: {exc}") from exc
    _normalize_commercial_proposal(data)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{'.'.join(map(str, e.path)) or '$'}: {e.message}" for e in errors[:8])
        raise LLMError(f"Insight schema validation failed: {details}")
    _validate_commercial_proposal(data)
    if segments is not None:
        _validate_evidence_references(data, segments)
    return data


def _gigachat_token() -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token
    response = httpx.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={"Authorization": f"Basic {GIGACHAT_AUTH_KEY}", "RqUID": str(uuid.uuid4())},
        data={"scope": GIGACHAT_SCOPE}, verify=GIGACHAT_CA_BUNDLE, timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    _token = body["access_token"]
    _token_expiry = body["expires_at"] / 1000
    return _token


def _require_local_browser_agent_gigachat(source_kind: str) -> None:
    """Fail closed before any network transport for a local-agent transcript."""

    if source_kind != "local_browser_agent":
        return
    if not isinstance(LLM_PROVIDER, str) or LLM_PROVIDER.strip().lower() != "gigachat":
        raise LLMError("local_browser_agent analysis requires LLM_PROVIDER=gigachat")
    if not isinstance(GIGACHAT_AUTH_KEY, str) or not GIGACHAT_AUTH_KEY.strip():
        raise LLMError("local_browser_agent analysis requires GIGACHAT_AUTH_KEY")


def _extract_gigachat_function_arguments(response_body: object) -> dict:
    """Extract one forced legacy GigaChat function-call payload fail closed."""

    if not isinstance(response_body, dict):
        raise LLMError("GigaChat response must be an object")
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError("GigaChat response has no valid choice")
    choice = choices[0]
    if choice.get("finish_reason") != "function_call":
        raise LLMError("GigaChat must finish with function_call")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMError("GigaChat function-call message is missing")
    function_call = message.get("function_call")
    if not isinstance(function_call, dict):
        raise LLMError("GigaChat function_call is missing")
    if function_call.get("name") != GIGACHAT_INSIGHT_FUNCTION_NAME:
        raise LLMError("GigaChat returned an unexpected function name")

    arguments = function_call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise LLMError("GigaChat function_call arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise LLMError("GigaChat function_call arguments must be a JSON object")
    return arguments


def _validate_gigachat_transport_arguments(arguments: object) -> dict:
    """Require the exact compact function-call contract before expansion."""

    if not isinstance(arguments, dict):
        raise LLMError("GigaChat transport arguments must be a JSON object")
    if not all(isinstance(name, str) for name in arguments):
        raise LLMError("GigaChat transport argument names must be strings")
    actual_fields = frozenset(arguments)
    missing = GIGACHAT_TRANSPORT_REQUIRED_FIELD_SET - actual_fields
    extra = actual_fields - GIGACHAT_TRANSPORT_REQUIRED_FIELD_SET
    if missing:
        raise LLMError(f"GigaChat transport arguments are missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise LLMError(f"GigaChat transport arguments contain unexpected fields: {', '.join(sorted(extra))}")

    for name in GIGACHAT_TRANSPORT_STRING_FIELDS:
        if not isinstance(arguments[name], str):
            raise LLMError(f"GigaChat transport {name} must be a string")
    for name in GIGACHAT_TRANSPORT_ARRAY_FIELDS:
        value = arguments[name]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LLMError(f"GigaChat transport {name} must be an array of strings")

    confidence = arguments["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or confidence < 0
        or confidence > 1
        or (isinstance(confidence, float) and not math.isfinite(confidence))
    ):
        raise LLMError("GigaChat transport confidence must be a finite number from 0 to 1")

    evidence = arguments["evidence"]
    if not isinstance(evidence, list):
        raise LLMError("GigaChat transport evidence must be an array")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise LLMError(f"GigaChat transport evidence[{index}] must be an object")
        if not all(isinstance(name, str) for name in item):
            raise LLMError(f"GigaChat transport evidence[{index}] field names must be strings")
        item_fields = frozenset(item)
        if item_fields != GIGACHAT_TRANSPORT_EVIDENCE_FIELD_SET:
            missing_item = GIGACHAT_TRANSPORT_EVIDENCE_FIELD_SET - item_fields
            extra_item = item_fields - GIGACHAT_TRANSPORT_EVIDENCE_FIELD_SET
            details = []
            if missing_item:
                details.append(f"missing {', '.join(sorted(missing_item))}")
            if extra_item:
                details.append(f"unexpected {', '.join(sorted(extra_item))}")
            raise LLMError(f"GigaChat transport evidence[{index}] has invalid fields: {'; '.join(details)}")
        if not isinstance(item["field"], str):
            raise LLMError(f"GigaChat transport evidence[{index}].field must be a string")
        ordinal = item["segment_ordinal"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise LLMError(f"GigaChat transport evidence[{index}].segment_ordinal must be a non-negative integer")
        quote = item["quote"]
        if not isinstance(quote, str) or not quote or len(quote) > 400:
            raise LLMError(f"GigaChat transport evidence[{index}].quote must be a non-empty string up to 400 chars")

    if not isinstance(arguments["commercial_proposal"], dict):
        raise LLMError("GigaChat transport commercial_proposal must be an object")
    return arguments


def _canonicalize_gigachat_transport_arguments(arguments: object, segments: list[dict]) -> dict:
    """Expand safe transport arguments into the complete persisted insight.

    GigaChat never supplies timecodes in its legacy function contract.  The
    worker derives them solely from the referenced stored ordinal, including a
    deliberate ``null/null`` pair for timeless reviewed segments.
    """

    arguments = _validate_gigachat_transport_arguments(arguments)
    source_by_ordinal: dict[int, dict] = {}
    for segment in _timeline_segments(segments):
        ordinal = segment["ordinal"]
        if ordinal in source_by_ordinal:
            raise LLMError("Transcript segment ordinal is not unique")
        source_by_ordinal[ordinal] = segment

    evidence = []
    for item in arguments["evidence"]:
        source = source_by_ordinal.get(item["segment_ordinal"])
        if source is None:
            raise LLMError("GigaChat transport evidence ordinal does not match a transcript segment")
        evidence.append({
            "field": item["field"],
            "segment_ordinal": item["segment_ordinal"],
            "segment_start_ms": source["started_ms"],
            "segment_end_ms": source["ended_ms"],
            "quote": item["quote"],
        })

    result = {
        name: None if arguments[name] == "" else arguments[name]
        for name in GIGACHAT_TRANSPORT_STRING_FIELDS
    }
    result.update({
        "budget_amount": None,
        "next_step_date": None,
        "quality_scores": {
            "discovery": None,
            "clarity": None,
            "objection_handling": None,
            "next_step": None,
        },
        "evidence": evidence,
        "confidence": arguments["confidence"],
        "commercial_proposal": arguments["commercial_proposal"],
    })
    result.update({name: list(arguments[name]) for name in GIGACHAT_TRANSPORT_ARRAY_FIELDS})
    return result


def summarize(transcript: str, segments: list[dict], *, source_kind: str = "legacy") -> dict:
    """Generate one validated insight.

    ``transcript`` remains an argument for compatibility with the legacy
    worker call path, but is deliberately never sent to a model.  The stored
    timeline is the only analysis input.
    """

    _require_local_browser_agent_gigachat(source_kind)
    analysis_input = build_analysis_input(segments)
    try:
        if LLM_PROVIDER == "gigachat":
            response = httpx.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {_gigachat_token()}"},
                json={
                    "model": GIGACHAT_MODEL,
                    "max_tokens": 1800,
                    "functions": [{
                        "name": GIGACHAT_INSIGHT_FUNCTION_NAME,
                        "description": "Submit one complete evidence-backed sales-call insight.",
                        "parameters": GIGACHAT_LEGACY_INSIGHT_FUNCTION_SCHEMA,
                    }],
                    "function_call": {"name": GIGACHAT_INSIGHT_FUNCTION_NAME},
                    "messages": [
                        {"role": "system", "content": GIGACHAT_FUNCTION_SYSTEM_PROMPT},
                        {"role": "user", "content": analysis_input},
                    ],
                },
                verify=GIGACHAT_CA_BUNDLE, timeout=90,
            )
            response.raise_for_status()
            transport_arguments = _extract_gigachat_function_arguments(response.json())
            raw = json.dumps(
                _canonicalize_gigachat_transport_arguments(transport_arguments, segments),
                ensure_ascii=False,
            )
        elif LLM_PROVIDER == "openai":
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": OPENAI_MODEL, "max_tokens": 1800, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": analysis_input}]},
                timeout=90,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        else:
            response = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "stream": False,
                      "format": INSIGHT_SCHEMA, "prompt": f"{SYSTEM_PROMPT}\n\n{analysis_input}"},
                timeout=240,
            )
            response.raise_for_status()
            raw = response.json()["response"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise LLMError(str(exc)) from exc
    return _parse(raw, segments=segments)
