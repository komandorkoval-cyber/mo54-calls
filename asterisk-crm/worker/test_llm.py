import json
import unittest
from unittest.mock import patch

from llm import LLMError, _parse, build_analysis_input, summarize


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


SEGMENTS = [{
    "ordinal": 0, "speaker": "customer", "started_ms": 1200, "ended_ms": 3800,
    "text": "Need rain protection. I will send a quote today.",
}]

VALID = {
    "summary": "Customer requested a quote.", "customer_intent": "Receive a proposal",
    "customer_need": "Price calculation", "product": None, "budget_amount": None,
    "timeline": None, "decision_maker": None, "lead_stage": "qualified",
    "lead_temperature": "warm", "objections": [], "manager_responses": [],
    "agreements": ["Send a quote"], "customer_promises": [],
    "company_promises": ["Send a quote"], "next_step": "Prepare a quote",
    "next_step_date": None, "next_step_owner": "manager", "loss_risk": "low",
    "outcome": "proposal_needed",
    "quality_scores": {"discovery": 3, "clarity": 4, "objection_handling": None, "next_step": 5},
    "recommendations": ["Clarify budget"],
    "evidence": [{
        "field": "next_step", "segment_ordinal": 0,
        "segment_start_ms": 1200, "segment_end_ms": 3800,
        "quote": "I will send a quote today",
    }],
    "confidence": 0.87,
    "commercial_proposal": commercial_fields(),
}


class InsightValidationTest(unittest.TestCase):
    def test_accepts_valid_payload_with_exact_timeline_evidence(self):
        self.assertEqual(
            _parse(json.dumps(VALID), segments=SEGMENTS)["lead_stage"],
            "qualified",
        )

    def test_strips_markdown_fence(self):
        self.assertEqual(
            _parse(f"```json\n{json.dumps(VALID)}\n```", segments=SEGMENTS)["confidence"],
            0.87,
        )

    def test_rejects_missing_field(self):
        invalid = VALID.copy()
        invalid.pop("evidence")
        with self.assertRaises(LLMError):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_unexpected_field(self):
        with self.assertRaises(LLMError):
            _parse(json.dumps({**VALID, "invented": True}), segments=SEGMENTS)

    def test_rejects_unsupported_budget_instead_of_inventing_a_fact(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["commercial_proposal"]["fields"]["estimated_budget_min"]["proposed_value"] = 120000
        with self.assertRaises(LLMError):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_accepts_supported_field_with_exact_segment_evidence(self):
        valid = json.loads(json.dumps(VALID))
        valid["commercial_proposal"]["fields"]["pain_primary"] = {
            "proposed_value": "Need rain protection", "confidence": 0.91,
            "evidence": [{
                "segment_ordinal": 0,
                "segment_start_ms": 1200, "segment_end_ms": 3800,
                "quote": "Need rain protection",
            }],
            "inference_status": "supported",
        }
        self.assertEqual(
            _parse(json.dumps(valid), segments=SEGMENTS)["commercial_proposal"]["fields"]["pain_primary"]["confidence"],
            0.91,
        )

    def test_rejects_evidence_for_an_interval_that_is_not_stored(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0]["segment_end_ms"] = 3700
        with self.assertRaisesRegex(LLMError, "identity"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_evidence_when_ordinal_does_not_match_the_stored_segment(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0]["segment_ordinal"] = 1
        with self.assertRaisesRegex(LLMError, "identity"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_evidence_without_segment_ordinal(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0].pop("segment_ordinal")
        with self.assertRaisesRegex(LLMError, "segment_ordinal"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_evidence_quote_that_is_not_a_literal_segment_substring(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0]["quote"] = "invented fact"
        with self.assertRaisesRegex(LLMError, "exact transcript substring"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_analysis_input_preserves_roles_and_segment_boundaries(self):
        body = json.loads(build_analysis_input(SEGMENTS))
        self.assertNotIn("transcript", body)
        self.assertEqual(body["segments"], [{
            "ordinal": 0, "role": "customer", "started_ms": 1200,
            "ended_ms": 3800, "text": SEGMENTS[0]["text"],
        }])

    def test_summarize_sends_the_timeline_and_validates_the_returned_evidence(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(VALID)}}]}

        def post(_url, **kwargs):
            captured.update(kwargs)
            return Response()

        with patch("llm.LLM_PROVIDER", "openai"), patch("llm.httpx.post", side_effect=post):
            result = summarize("canonical transcript", SEGMENTS)

        self.assertEqual(result["summary"], VALID["summary"])
        body = json.loads(captured["json"]["messages"][1]["content"])
        self.assertNotIn("transcript", body)
        self.assertEqual(body["segments"][0]["role"], "customer")
        self.assertEqual(body["segments"][0]["started_ms"], 1200)

    def test_local_browser_agent_refuses_openai_before_any_http_request(self):
        with patch("llm.LLM_PROVIDER", "openai"), patch("llm.GIGACHAT_AUTH_KEY", "configured"), \
                patch("llm.httpx.post") as post:
            with self.assertRaisesRegex(LLMError, "LLM_PROVIDER=gigachat"):
                summarize("divergent flattened text", SEGMENTS, source_kind="local_browser_agent")
        post.assert_not_called()

    def test_local_browser_agent_refuses_missing_gigachat_key_before_any_http_request(self):
        with patch("llm.LLM_PROVIDER", "gigachat"), patch("llm.GIGACHAT_AUTH_KEY", "  "), \
                patch("llm.httpx.post") as post:
            with self.assertRaisesRegex(LLMError, "GIGACHAT_AUTH_KEY"):
                summarize("divergent flattened text", SEGMENTS, source_kind="local_browser_agent")
        post.assert_not_called()

    def test_local_browser_agent_gigachat_receives_only_stored_segments(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(VALID)}}]}

        def post(_url, **kwargs):
            captured.update(kwargs)
            return Response()

        with patch("llm.LLM_PROVIDER", "gigachat"), patch("llm.GIGACHAT_AUTH_KEY", "configured"), \
                patch("llm._gigachat_token", return_value="token"), patch("llm.httpx.post", side_effect=post):
            result = summarize("a deliberately divergent flattened transcript", SEGMENTS,
                               source_kind="local_browser_agent")

        self.assertEqual(result["summary"], VALID["summary"])
        body = json.loads(captured["json"]["messages"][1]["content"])
        self.assertEqual(set(body), {"segments"})
        self.assertEqual(body["segments"][0]["text"], SEGMENTS[0]["text"])


if __name__ == "__main__":
    unittest.main()
