import json
import unittest

from llm import LLMError, _parse


def unknown(value=None):
    return {"proposed_value": value, "confidence": None, "evidence": [], "inference_status": "unknown"}


def commercial_fields():
    fields = {
        "qualification_segment": unknown(), "estimated_budget_min": unknown(), "estimated_budget_max": unknown(),
        "budget_range": unknown(), "pain_primary": unknown(), "pain_secondary": unknown([]),
        "customer_quote": unknown(), "decision_makers": unknown([]), "decision_maker_status": unknown(),
        "alternative_considered": unknown(), "alternative_reason": unknown(), "desired_install_date": unknown(),
        "desired_install_period": unknown(), "next_contact_at": unknown(), "suggested_stage": unknown(),
    }
    return {"fields": fields}

VALID = {
    "summary": "Клиент запросил расчёт.", "customer_intent": "Получить предложение",
    "customer_need": "Расчёт стоимости", "product": None, "budget_amount": None,
    "timeline": None, "decision_maker": None, "lead_stage": "qualified",
    "lead_temperature": "warm", "objections": [], "manager_responses": [],
    "agreements": ["Отправить расчёт"], "customer_promises": [],
    "company_promises": ["Отправить расчёт"], "next_step": "Подготовить расчёт",
    "next_step_date": None, "next_step_owner": "manager", "loss_risk": "low",
    "outcome": "proposal_needed",
    "quality_scores": {"discovery": 3, "clarity": 4, "objection_handling": None, "next_step": 5},
    "recommendations": ["Уточнить бюджет"],
    "evidence": [{"field": "next_step", "quote": "Пришлю расчёт сегодня"}],
    "confidence": 0.87,
    "commercial_proposal": commercial_fields(),
}


class InsightValidationTest(unittest.TestCase):
    def test_accepts_valid_payload(self):
        self.assertEqual(_parse(json.dumps(VALID, ensure_ascii=False))["lead_stage"], "qualified")

    def test_strips_markdown_fence(self):
        self.assertEqual(_parse(f"```json\n{json.dumps(VALID)}\n```")["confidence"], 0.87)

    def test_rejects_missing_field(self):
        invalid = VALID.copy()
        invalid.pop("evidence")
        with self.assertRaises(LLMError):
            _parse(json.dumps(invalid))

    def test_rejects_unexpected_field(self):
        with self.assertRaises(LLMError):
            _parse(json.dumps({**VALID, "invented": True}))

    def test_rejects_unsupported_budget_instead_of_inventing_a_fact(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["commercial_proposal"]["fields"]["estimated_budget_min"]["proposed_value"] = 120000
        with self.assertRaises(LLMError):
            _parse(json.dumps(invalid))

    def test_accepts_supported_field_with_segment_evidence(self):
        valid = json.loads(json.dumps(VALID))
        valid["commercial_proposal"]["fields"]["pain_primary"] = {
            "proposed_value": "Нужна защита от дождя", "confidence": 0.91,
            "evidence": [{"segment_start_ms": 1200, "segment_end_ms": 3800, "quote": "Нам нужна защита от дождя"}],
            "inference_status": "supported",
        }
        self.assertEqual(_parse(json.dumps(valid))["commercial_proposal"]["fields"]["pain_primary"]["confidence"], 0.91)


if __name__ == "__main__":
    unittest.main()
