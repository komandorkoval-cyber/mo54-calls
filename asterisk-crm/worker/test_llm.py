import json
import unittest

from llm import LLMError, _parse

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


if __name__ == "__main__":
    unittest.main()
