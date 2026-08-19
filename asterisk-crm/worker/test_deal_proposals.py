import os
import sys
import types
import unittest
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")

if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.Json = lambda value: value
    extras.RealDictCursor = object
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

from db import AI_DEAL_UPDATE_FIELDS, DEAL_SNAPSHOT_FIELDS, build_deal_update_payload, unambiguous_accessible_deal_for_call


def unknown(value=None):
    return {"proposed_value": value, "confidence": None, "evidence": [], "inference_status": "unknown"}


def insight_with_supported_pain():
    fields = {field: unknown([] if field in {"pain_secondary", "decision_makers"} else None)
              for field in AI_DEAL_UPDATE_FIELDS}
    fields["pain_primary"] = {
        "proposed_value": "Нужна защита от дождя", "confidence": 0.91,
        "evidence": [{"segment_start_ms": 1200, "segment_end_ms": 3800, "quote": "Нам нужна защита от дождя"}],
        "inference_status": "supported",
    }
    return {"commercial_proposal": {"fields": fields}}


class DealCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.executed = []

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return next(self.responses)


class DealUpdateProposalTests(unittest.TestCase):
    def snapshot(self, deal_id):
        values = {field: None for field in DEAL_SNAPSHOT_FIELDS}
        values["stage"] = "qualified"
        values["pain_primary"] = "Старое значение"
        return tuple([deal_id, *[values[field] for field in DEAL_SNAPSHOT_FIELDS]])

    def test_existing_directly_linked_deal_becomes_deal_update_with_evidence(self):
        deal_id = uuid4()
        cursor = DealCursor([[self.snapshot(deal_id)]])
        target = unambiguous_accessible_deal_for_call(cursor, 42)
        payload, evidence = build_deal_update_payload(insight_with_supported_pain(), target)
        self.assertEqual(target["id"], deal_id)
        self.assertEqual(payload["base_values"]["pain_primary"], "Старое значение")
        self.assertEqual(payload["proposed_fields"]["pain_primary"]["proposed_value"], "Нужна защита от дождя")
        self.assertEqual(evidence[0]["segment_start_ms"], 1200)
        self.assertEqual(len(cursor.executed), 1)

    def test_ambiguous_contact_relation_does_not_guess_a_target_deal(self):
        cursor = DealCursor([[], [self.snapshot(uuid4()), self.snapshot(uuid4())]])
        self.assertIsNone(unambiguous_accessible_deal_for_call(cursor, 42))

    def test_unknown_facts_do_not_create_an_update_payload(self):
        empty = {"commercial_proposal": {"fields": {
            field: unknown([] if field in {"pain_secondary", "decision_makers"} else None)
            for field in AI_DEAL_UPDATE_FIELDS
        }}}
        target = {"id": uuid4(), **{field: None for field in DEAL_SNAPSHOT_FIELDS}}
        self.assertIsNone(build_deal_update_payload(empty, target))


if __name__ == "__main__":
    unittest.main()
