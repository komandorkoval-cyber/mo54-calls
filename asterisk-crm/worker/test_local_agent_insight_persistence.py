import os
import sys
import types
import unittest
from contextlib import contextmanager
from uuid import uuid4
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")

if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.Json = lambda value: value
    extras.RealDictCursor = object
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

from db import AI_DEAL_UPDATE_FIELDS, DEAL_SNAPSHOT_FIELDS, save_insight


def unknown(value=None):
    return {"proposed_value": value, "confidence": None, "evidence": [], "inference_status": "unknown"}


def insight_with_supported_field():
    fields = {
        field: unknown([] if field in {"pain_secondary", "decision_makers"} else None)
        for field in AI_DEAL_UPDATE_FIELDS
    }
    fields["pain_primary"] = {
        "proposed_value": "Need rain protection",
        "confidence": 0.91,
        "evidence": [{
            "segment_ordinal": 4,
            "segment_start_ms": 1200,
            "segment_end_ms": 3800,
            "quote": "Need rain protection",
        }],
        "inference_status": "supported",
    }
    return {
        "summary": "Customer wants rain protection",
        "customer_intent": None,
        "customer_need": "Rain protection",
        "product": "Terrace",
        "budget_amount": None,
        "timeline": None,
        "decision_maker": None,
        "lead_stage": "qualified",
        "lead_temperature": "warm",
        "objections": [],
        "manager_responses": [],
        "agreements": [],
        "customer_promises": [],
        "company_promises": [],
        "next_step": "Prepare a quote",
        "next_step_date": None,
        "next_step_owner": "manager",
        "loss_risk": "low",
        "outcome": "proposal_needed",
        "quality_scores": {"discovery": 3, "clarity": 3, "objection_handling": 3, "next_step": 3},
        "recommendations": [],
        "evidence": [],
        "confidence": 0.91,
        "commercial_proposal": {"fields": fields},
    }


class Cursor:
    def __init__(self, deal_id):
        self.deal_id = deal_id
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        sql = self.executed[-1][0]
        if "coalesce(max(version),0)+1 FROM call_insights" in sql:
            return (1,)
        if "INSERT INTO call_insights" in sql:
            return (uuid4(),)
        raise AssertionError(f"Unexpected fetchone for SQL: {sql}")

    def fetchall(self):
        sql = self.executed[-1][0]
        if "FROM call_deals" not in sql:
            raise AssertionError(f"Unexpected fetchall for SQL: {sql}")
        values = {field: None for field in DEAL_SNAPSHOT_FIELDS}
        values["stage"] = "qualified"
        return [tuple([self.deal_id, *[values[field] for field in DEAL_SNAPSHOT_FIELDS]])]


class Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def cursor(self, **_kwargs):
        return self.cursor_value


class LocalAgentInsightPersistenceTests(unittest.TestCase):
    def _save(self, *, source_kind, transcript_id=None):
        cursor = Cursor(uuid4())

        @contextmanager
        def fake_connection():
            yield Connection(cursor)

        with patch("db.connection", fake_connection), patch("db.Json", side_effect=lambda value: value):
            save_insight(
                42,
                insight_with_supported_field(),
                "test-model",
                "test-prompt",
                source_kind=source_kind,
                transcript_id=transcript_id,
            )
        return cursor

    def test_local_agent_writes_only_technical_call_state_and_v2_review_draft(self):
        transcript_id = uuid4()
        cursor = self._save(source_kind="local_browser_agent", transcript_id=transcript_id)

        call_updates = [(sql, params) for sql, params in cursor.executed if "UPDATE calls SET" in sql]
        self.assertEqual(len(call_updates), 1)
        call_sql, call_params = call_updates[0]
        self.assertIn("processing_status='ready'", call_sql)
        self.assertNotIn("theme=", call_sql)
        self.assertNotIn("client_request=", call_sql)
        self.assertNotIn("agreements=", call_sql)
        self.assertNotIn("amount=", call_sql)
        self.assertNotIn("next_step=", call_sql)
        self.assertNotIn("next_date=", call_sql)
        self.assertNotIn("raw_json=", call_sql)
        self.assertNotIn("status='stored'", call_sql)
        self.assertEqual(call_params, (42,))

        draft_sql, draft_params = next(
            (sql, params) for sql, params in cursor.executed if "'deal_update'" in sql
        )
        self.assertIn("proposal_schema_version", draft_sql)
        self.assertEqual(draft_params[-1], "p0-deal-update-v2-evidence")
        payload = draft_params[3]
        evidence = payload["proposed_fields"]["pain_primary"]["evidence"][0]
        self.assertEqual(evidence["transcript_id"], str(transcript_id))
        self.assertEqual(evidence["segment_ordinal"], 4)
        self.assertEqual(draft_params[4][0]["transcript_id"], str(transcript_id))
        self.assertEqual(draft_params[4][0]["segment_ordinal"], 4)

    def test_local_agent_refuses_to_create_an_unbound_draft(self):
        with self.assertRaisesRegex(ValueError, "transcript_id"):
            self._save(source_kind="local_browser_agent")

    def test_legacy_path_retains_business_projection_and_v1_draft_shape(self):
        cursor = self._save(source_kind="legacy")
        call_sql, _call_params = next(
            (sql, params) for sql, params in cursor.executed if "UPDATE calls SET" in sql
        )
        self.assertIn("theme=", call_sql)
        self.assertIn("raw_json=", call_sql)
        draft_sql, draft_params = next(
            (sql, params) for sql, params in cursor.executed if "'deal_update'" in sql
        )
        self.assertIn("proposal_schema_version", draft_sql)
        self.assertEqual(draft_params[-1], "p0-deal-update-v1")
        evidence = draft_params[3]["proposed_fields"]["pain_primary"]["evidence"][0]
        self.assertNotIn("transcript_id", evidence)
        self.assertNotIn("segment_ordinal", evidence)


if __name__ == "__main__":
    unittest.main()
