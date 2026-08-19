import json
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
PROMPT_VERSION = "sales-v1.1"
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


def _field_proposal_schema(value_schema: dict) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["proposed_value", "confidence", "evidence", "inference_status"],
        "properties": {
            "proposed_value": value_schema,
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "evidence": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["segment_start_ms", "segment_end_ms", "quote"],
                "properties": {
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
                "type": "object", "additionalProperties": False, "required": ["field", "quote"],
                "properties": {"field": {"type": "string"}, "quote": {"type": "string", "maxLength": 400}},
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
validator = Draft202012Validator(INSIGHT_SCHEMA)

SYSTEM_PROMPT = f"""Ты — аналитик продаж небольшой компании. Анализируй только факты из транскрипта.
Не выдумывай сведения: неизвестные скалярные значения указывай как null, списки — как [].
Верни только JSON, соответствующий этой JSON Schema:
{json.dumps(INSIGHT_SCHEMA, ensure_ascii=False)}
Оценки quality_scores: 0 — навык отсутствует, 5 — выполнен отлично. Каждая важная
квалификация должна иметь короткую подтверждающую цитату в evidence. Рекомендации
должны быть конкретными и относиться к следующему разговору."""


class LLMError(RuntimeError):
    pass


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
        for item in evidence:
            if item["segment_start_ms"] is None and item["segment_end_ms"] is None:
                raise LLMError(f"{name}: evidence must reference a transcript segment")


def _parse(raw: str) -> dict:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Invalid JSON: {exc}") from exc
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{'.'.join(map(str, e.path)) or '$'}: {e.message}" for e in errors[:8])
        raise LLMError(f"Insight schema validation failed: {details}")
    _validate_commercial_proposal(data)
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


def summarize(transcript: str) -> dict:
    try:
        if LLM_PROVIDER == "gigachat":
            response = httpx.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {_gigachat_token()}"},
                json={"model": GIGACHAT_MODEL, "max_tokens": 1800,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": transcript}]},
                verify=GIGACHAT_CA_BUNDLE, timeout=90,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        elif LLM_PROVIDER == "openai":
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": OPENAI_MODEL, "max_tokens": 1800, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": transcript}]},
                timeout=90,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        else:
            response = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "stream": False,
                      "format": INSIGHT_SCHEMA, "prompt": f"{SYSTEM_PROMPT}\n\n{transcript}"},
                timeout=240,
            )
            response.raise_for_status()
            raw = response.json()["response"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise LLMError(str(exc)) from exc
    return _parse(raw)
