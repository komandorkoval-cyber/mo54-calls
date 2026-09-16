import json
import unittest
from unittest.mock import patch

from llm import (
    GIGACHAT_LEGACY_INSIGHT_FUNCTION_SCHEMA,
    GIGACHAT_INSIGHT_FUNCTION_NAME,
    GIGACHAT_TRANSPORT_REQUIRED_FIELDS,
    INSIGHT_SCHEMA,
    LLMError,
    _canonicalize_gigachat_transport_arguments,
    _extract_gigachat_function_arguments,
    _parse,
    build_analysis_input,
    summarize,
)


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

TRANSPORT_VALID = {
    "summary": "Customer requested a quote.",
    "customer_intent": "Receive a proposal",
    "customer_need": "Price calculation",
    "product": "",
    "timeline": "",
    "decision_maker": "",
    "lead_stage": "qualified",
    "lead_temperature": "warm",
    "next_step": "Prepare a quote",
    "next_step_owner": "manager",
    "loss_risk": "low",
    "outcome": "proposal_needed",
    "objections": [],
    "manager_responses": [],
    "agreements": ["Send a quote"],
    "customer_promises": [],
    "company_promises": ["Send a quote"],
    "recommendations": ["Clarify budget"],
    "confidence": 0.87,
    "evidence": [{
        "field": "next_step",
        "segment_ordinal": 0,
        "quote": "I will send a quote today",
    }],
    "commercial_proposal": {},
}


def gigachat_function_response(arguments, *, finish_reason="function_call",
                               name=GIGACHAT_INSIGHT_FUNCTION_NAME):
    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"function_call": {"name": name, "arguments": arguments}},
        }],
    }


def contains_list_valued_type(schema):
    if isinstance(schema, dict):
        if isinstance(schema.get("type"), list):
            return True
        return any(contains_list_valued_type(value) for value in schema.values())
    if isinstance(schema, list):
        return any(contains_list_valued_type(value) for value in schema)
    return False


class InsightValidationTest(unittest.TestCase):
    def test_accepts_valid_payload_with_exact_timeline_evidence(self):
        self.assertEqual(
            _parse(json.dumps(VALID), segments=SEGMENTS)["lead_stage"],
            "qualified",
        )

    def test_accepts_exact_null_timecode_pair_for_a_timeless_segment(self):
        timeless = [{
            **SEGMENTS[0],
            "started_ms": None,
            "ended_ms": None,
        }]
        valid = json.loads(json.dumps(VALID))
        valid["evidence"][0]["segment_start_ms"] = None
        valid["evidence"][0]["segment_end_ms"] = None
        self.assertEqual(
            _parse(json.dumps(valid), segments=timeless)["summary"],
            VALID["summary"],
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

    def test_preserves_a_complete_evidence_backed_commercial_proposal(self):
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

        parsed = _parse(json.dumps(valid), segments=SEGMENTS)

        self.assertEqual(parsed["commercial_proposal"], valid["commercial_proposal"])

    def test_discards_missing_empty_partial_or_malformed_commercial_proposal(self):
        cases = {
            "missing": lambda value: value.pop("commercial_proposal"),
            "empty": lambda value: value.update({"commercial_proposal": {}}),
            "partial": lambda value: value.update({"commercial_proposal": {
                "fields": {"pain_primary": unknown()},
            }}),
            "malformed": lambda value: value.update({"commercial_proposal": {
                "unexpected": True,
            }}),
            "wrong_type": lambda value: value.update({"commercial_proposal": []}),
        }
        for label, break_proposal in cases.items():
            with self.subTest(label=label):
                invalid = json.loads(json.dumps(VALID))
                # A valid-looking actionable value must not survive a partial
                # or malformed envelope.
                invalid["commercial_proposal"]["fields"]["pain_primary"] = {
                    "proposed_value": "Need rain protection", "confidence": 0.91,
                    "evidence": [{
                        "segment_ordinal": 0,
                        "segment_start_ms": 1200, "segment_end_ms": 3800,
                        "quote": "Need rain protection",
                    }],
                    "inference_status": "supported",
                }
                break_proposal(invalid)

                parsed = _parse(json.dumps(invalid), segments=SEGMENTS)

                self.assertEqual(parsed["commercial_proposal"], commercial_fields())

    def test_rejects_invalid_top_level_evidence_after_commercial_recovery(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["commercial_proposal"] = {"fields": {"pain_primary": unknown()}}
        invalid["evidence"][0]["quote"] = "invented fact"

        with self.assertRaisesRegex(LLMError, "exact transcript substring"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_unexpected_field(self):
        with self.assertRaises(LLMError):
            _parse(json.dumps({**VALID, "invented": True}), segments=SEGMENTS)

    def test_discards_a_semantically_invalid_commercial_value(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["commercial_proposal"]["fields"]["estimated_budget_min"]["proposed_value"] = 120000

        parsed = _parse(json.dumps(invalid), segments=SEGMENTS)

        self.assertEqual(parsed["commercial_proposal"], commercial_fields())

    def test_rejects_evidence_for_an_interval_that_is_not_stored(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0]["segment_end_ms"] = 3700
        with self.assertRaisesRegex(LLMError, "identity"):
            _parse(json.dumps(invalid), segments=SEGMENTS)

    def test_rejects_evidence_with_a_partial_null_timecode_pair(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["evidence"][0]["segment_start_ms"] = None
        with self.assertRaisesRegex(LLMError, "pair of nulls"):
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

    def test_analysis_input_preserves_a_timeless_segment_as_a_null_pair(self):
        body = json.loads(build_analysis_input([{
            **SEGMENTS[0],
            "started_ms": None,
            "ended_ms": None,
        }]))
        self.assertEqual(body["segments"][0]["started_ms"], None)
        self.assertEqual(body["segments"][0]["ended_ms"], None)

    def test_analysis_input_rejects_partial_source_timecode(self):
        with self.assertRaisesRegex(LLMError, "pair of nulls"):
            build_analysis_input([{**SEGMENTS[0], "started_ms": None}])

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

    def test_gigachat_forces_one_insight_function_and_accepts_object_arguments(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return gigachat_function_response(TRANSPORT_VALID)

        def post(_url, **kwargs):
            captured.update(kwargs)
            return Response()

        with patch("llm.LLM_PROVIDER", "gigachat"), patch("llm.GIGACHAT_AUTH_KEY", "configured"), \
                patch("llm._gigachat_token", return_value="token"), patch("llm.httpx.post", side_effect=post):
            result = summarize("canonical transcript", SEGMENTS)

        self.assertEqual(result["summary"], VALID["summary"])
        self.assertIsNone(result["product"])
        self.assertIsNone(result["budget_amount"])
        self.assertIsNone(result["next_step_date"])
        self.assertEqual(result["quality_scores"], {
            "discovery": None, "clarity": None,
            "objection_handling": None, "next_step": None,
        })
        self.assertEqual(result["evidence"][0]["segment_start_ms"], 1200)
        self.assertEqual(result["evidence"][0]["segment_end_ms"], 3800)
        self.assertEqual(result["commercial_proposal"], commercial_fields())
        request = captured["json"]
        self.assertEqual(request["function_call"], {"name": GIGACHAT_INSIGHT_FUNCTION_NAME})
        self.assertEqual(len(request["functions"]), 1)
        self.assertEqual(request["functions"][0]["name"], GIGACHAT_INSIGHT_FUNCTION_NAME)
        parameters = request["functions"][0]["parameters"]
        self.assertEqual(parameters, GIGACHAT_LEGACY_INSIGHT_FUNCTION_SCHEMA)
        self.assertNotEqual(parameters, INSIGHT_SCHEMA)
        self.assertFalse(contains_list_valued_type(parameters))
        self.assertEqual(parameters["required"], list(GIGACHAT_TRANSPORT_REQUIRED_FIELDS))
        self.assertEqual(set(parameters["properties"]), set(GIGACHAT_TRANSPORT_REQUIRED_FIELDS))
        self.assertEqual(parameters["properties"]["summary"], {"type": "string"})
        self.assertEqual(parameters["properties"]["confidence"], {"type": "number"})
        self.assertEqual(parameters["properties"]["evidence"], {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "segment_ordinal": {"type": "integer"},
                    "quote": {"type": "string"},
                },
            },
        })
        self.assertEqual(parameters["properties"]["commercial_proposal"], {
            "type": "object", "properties": {},
        })
        self.assertNotIn("budget_amount", parameters["properties"])
        self.assertNotIn("next_step_date", parameters["properties"])
        self.assertNotIn("quality_scores", parameters["properties"])
        self.assertNotIn("response_format", request)
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(set(payload), {"segments"})

    def test_gigachat_normalizes_string_function_arguments(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return gigachat_function_response(json.dumps(TRANSPORT_VALID))

        with patch("llm.LLM_PROVIDER", "gigachat"), patch("llm.GIGACHAT_AUTH_KEY", "configured"), \
                patch("llm._gigachat_token", return_value="token"), patch("llm.httpx.post", return_value=Response()):
            result = summarize("canonical transcript", SEGMENTS)

        self.assertEqual(result["summary"], VALID["summary"])

    def test_gigachat_transport_canonicalizes_timeless_evidence(self):
        timeless = [{
            **SEGMENTS[0],
            "started_ms": None,
            "ended_ms": None,
        }]

        canonical = _canonicalize_gigachat_transport_arguments(TRANSPORT_VALID, timeless)
        parsed = _parse(json.dumps(canonical), segments=timeless)

        self.assertEqual(parsed["evidence"][0]["segment_ordinal"], 0)
        self.assertIsNone(parsed["evidence"][0]["segment_start_ms"])
        self.assertIsNone(parsed["evidence"][0]["segment_end_ms"])
        self.assertEqual(parsed["quality_scores"], {
            "discovery": None, "clarity": None,
            "objection_handling": None, "next_step": None,
        })

    def test_gigachat_transport_rejects_extra_missing_or_bad_arguments(self):
        cases = {
            "missing": lambda value: value.pop("summary"),
            "extra": lambda value: value.update({"budget_amount": 100000}),
            "bad_string": lambda value: value.update({"summary": None}),
            "bad_confidence": lambda value: value.update({"confidence": "0.87"}),
            "bad_evidence": lambda value: value["evidence"][0].update({"segment_start_ms": 1200}),
            "bad_commercial": lambda value: value.update({"commercial_proposal": []}),
        }
        for label, break_transport in cases.items():
            with self.subTest(label=label):
                invalid = json.loads(json.dumps(TRANSPORT_VALID))
                break_transport(invalid)
                with self.assertRaisesRegex(LLMError, "GigaChat transport"):
                    _canonicalize_gigachat_transport_arguments(invalid, SEGMENTS)

    def test_gigachat_transport_rejects_nonliteral_evidence_after_time_enrichment(self):
        invalid = json.loads(json.dumps(TRANSPORT_VALID))
        invalid["evidence"][0]["quote"] = "invented fact"

        canonical = _canonicalize_gigachat_transport_arguments(invalid, SEGMENTS)

        self.assertEqual(canonical["evidence"][0]["segment_start_ms"], 1200)
        self.assertEqual(canonical["evidence"][0]["segment_end_ms"], 3800)
        with self.assertRaisesRegex(LLMError, "exact transcript substring"):
            _parse(json.dumps(canonical), segments=SEGMENTS)

    def test_gigachat_rejects_wrong_finish_name_or_arguments(self):
        cases = {
            "finish": (gigachat_function_response(TRANSPORT_VALID, finish_reason="stop"), "finish with function_call"),
            "name": (gigachat_function_response(TRANSPORT_VALID, name="unexpected"), "unexpected function name"),
            "not_object": (gigachat_function_response([]), "arguments must be a JSON object"),
            "not_json": (gigachat_function_response("not-json"), "arguments are not valid JSON"),
        }
        for label, (body, message) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(LLMError, message):
                    _extract_gigachat_function_arguments(body)

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
                return gigachat_function_response(TRANSPORT_VALID)

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
