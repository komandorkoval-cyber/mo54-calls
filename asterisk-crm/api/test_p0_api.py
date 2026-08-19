"""Handler-level P0 API policy tests; database behavior is covered separately."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
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


class CashflowOperationTests(unittest.TestCase):
    def setUp(self):
        self.user = app.User(id=uuid4(), email="owner@example.test", display_name="Owner", role="manager")
        self.deal_id = uuid4()
        self.deal = {"id": self.deal_id, "owner_id": self.user.id}

    def test_payment_and_refund_are_visible_in_ledger_totals(self):
        movements = [
            {"id": uuid4(), "kind": "customer_incoming", "amount": 150000},
            {"id": uuid4(), "kind": "customer_refund", "amount": 10000},
        ]
        with patch.object(app, "fetch_one", return_value=self.deal), patch.object(
            app, "fetch_all", side_effect=[movements, []]
        ):
            result = app.deal_cashflow(self.deal_id, self.user)
        self.assertEqual(result["confirmed_customer_cash"], 150000)
        self.assertEqual(result["refunds"], 10000)
        self.assertEqual(result["net_confirmed_customer_cash"], 140000)
        self.assertEqual(result["safe_cash"], 140000)

    def test_create_payment_writes_confirmed_append_only_row_and_audits(self):
        movement_id = uuid4()
        captured: dict[str, object] = {}

        def fake_execute(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return {"id": movement_id, "kind": "customer_incoming", "amount": 150000}

        with patch.object(app, "fetch_one", return_value=self.deal), patch.object(
            app, "execute", side_effect=fake_execute
        ), patch.object(app, "audit") as audit:
            result = app.create_cash_movement(
                self.deal_id, app.CashMovementCreate(kind="customer_incoming", amount=150000), self.user
            )
        self.assertEqual(result["id"], movement_id)
        self.assertIn("confirmed_at", captured["sql"])
        self.assertEqual(captured["params"][1], "customer_incoming")
        audit.assert_called_once()

    def test_open_obligation_reduces_safe_cash_and_settled_cost_replaces_it(self):
        payment = {"id": uuid4(), "kind": "customer_incoming", "amount": 150000}
        open_obligation = {"id": uuid4(), "amount": 60000, "status": "open"}
        settled_outflow = {"id": uuid4(), "kind": "realized_cost_outflow", "amount": 60000}
        settled_obligation = {**open_obligation, "status": "settled", "settled_movement_id": settled_outflow["id"]}
        with patch.object(app, "fetch_one", return_value=self.deal), patch.object(
            app, "fetch_all", side_effect=[[payment], [open_obligation]]
        ):
            before = app.deal_cashflow(self.deal_id, self.user)
        with patch.object(app, "fetch_one", return_value=self.deal), patch.object(
            app, "fetch_all", side_effect=[[payment, settled_outflow], [settled_obligation]]
        ):
            after = app.deal_cashflow(self.deal_id, self.user)
        self.assertEqual(before["open_obligations"], 60000)
        self.assertEqual(before["safe_cash"], 90000)
        self.assertEqual(after["open_obligations"], 0)
        self.assertEqual(after["realized_costs"], 60000)
        self.assertEqual(after["safe_cash"], 90000)

    def test_confirmed_movement_has_no_normal_edit_or_delete_route(self):
        paths = {route.path for route in app.app.routes}
        self.assertNotIn("/api/deals/{deal_id}/cash-movements/{movement_id}", paths)
        self.assertIn("/api/deals/{deal_id}/cash-movements/{movement_id}/reverse", paths)

    def test_reversal_creates_compensating_row_without_changing_original(self):
        movement_id, reversal_id = uuid4(), uuid4()
        original = {"id": movement_id, "deal_id": self.deal_id, "kind": "customer_incoming", "amount": 150000}
        original_copy = dict(original)
        with patch.object(app, "fetch_one", side_effect=[self.deal, original]), patch.object(
            app, "execute", return_value={"id": reversal_id, "kind": "customer_refund", "amount": 150000}
        ) as execute, patch.object(app, "audit") as audit:
            result = app.reverse_cash_movement(
                self.deal_id, movement_id, app.CashMovementReverse(reason="duplicate payment"), self.user
            )
        self.assertEqual(result["id"], reversal_id)
        self.assertEqual(original, original_copy)
        self.assertIn("reversal_of_movement_id", execute.call_args.args[0])
        self.assertEqual(execute.call_args.args[1][2], "customer_refund")
        audit.assert_called_once()

    def test_manager_cannot_write_cashflow_for_foreign_deal(self):
        with patch.object(app, "fetch_one", return_value={"id": self.deal_id, "owner_id": uuid4()}):
            with self.assertRaises(HTTPException) as error:
                app.create_cash_movement(
                    self.deal_id, app.CashMovementCreate(kind="customer_incoming", amount=1), self.user
                )
        self.assertEqual(error.exception.status_code, 404)

    def test_settled_obligation_cannot_be_edited(self):
        obligation = {"id": uuid4(), "deal_id": self.deal_id, "status": "settled", "amount": 60000}
        with patch.object(app, "fetch_one", side_effect=[self.deal, obligation]):
            with self.assertRaises(HTTPException) as error:
                app.update_open_cost_obligation(
                    self.deal_id, obligation["id"], app.CostObligationPatch(amount=1), self.user
                )
        self.assertEqual(error.exception.status_code, 409)


class CashflowWorkspaceMarkupTests(unittest.TestCase):
    def test_deal_workspace_exposes_all_operational_cashflow_paths(self):
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        for required in (
            'id="cash-movement-form"',
            'value="customer_incoming"',
            'value="customer_refund"',
            'value="realized_cost_outflow"',
            'id="obligation-create-form"',
            'data-obligation-form=',
            'data-settle-obligation=',
            'data-reverse-movement=',
            'append-only',
            '/cashflow',
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
