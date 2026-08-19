"""Handler-level P0 API policy tests; database behavior is covered separately."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class DealTransitionPolicyTests(unittest.TestCase):
    def test_legacy_and_p0_stages_are_accepted_by_create_contract(self):
        contact_id = uuid4()
        self.assertEqual(app.DealCreate(contact_id=contact_id, title="Legacy", stage="proposal").stage, "proposal")
        self.assertEqual(app.DealCreate(contact_id=contact_id, title="P0", stage="installed").stage, "installed")

    def test_historical_row_is_not_rejected_without_a_new_transition(self):
        app.deal_stage_transition_allowed({"stage": "won", "loss_reason": None}, {"quoted_price": 120000})

    def test_new_terminal_and_pending_transitions_require_their_fields(self):
        before = {"stage": "proposal", "loss_reason": None, "disqualification_reason": None, "next_contact_at": None}
        for stage in ("closed_lost", "disqualified", "decision_pending"):
            with self.assertRaises(HTTPException):
                app.deal_stage_transition_allowed(before, {"stage": stage})
        app.deal_stage_transition_allowed(before, {"stage": "closed_lost", "loss_reason": "price_too_high"})
        app.deal_stage_transition_allowed(before, {"stage": "disqualified", "disqualification_reason": "small_object"})
        app.deal_stage_transition_allowed(before, {"stage": "decision_pending", "next_contact_at": "2026-08-21T10:00:00+07:00"})


class DashboardPolicyTests(unittest.TestCase):
    def test_dashboard_derives_safe_cash_and_remaining_goal(self):
        user = app.User(id=uuid4(), email="a@example.test", display_name="A", role="admin")
        expected = {
            "goal_owner_income": 800000, "earned_owner_income": 250000,
            "projected_owner_income": 500000, "net_confirmed_customer_cash": 300000,
            "realized_cost_outflows": 80000, "open_reserved_obligations": 70000,
            "other_reserved_cash": 10000,
        }
        original = app.fetch_one
        app.fetch_one = lambda *_args, **_kwargs: dict(expected)
        try:
            result = app.season_dashboard(user)
        finally:
            app.fetch_one = original
        self.assertEqual(result["safe_cash"], 140000)
        self.assertEqual(result["remaining_to_goal"], 550000)


if __name__ == "__main__":
    unittest.main()
