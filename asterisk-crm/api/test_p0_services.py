from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from p0_services import (  # noqa: E402
    EconomicsInputs,
    EconomicsSettings,
    calculate_economics,
    calculate_safe_cash,
    recognize_income_for_stage,
    semantic_funnel_stage,
    settle_cost_obligation,
)


SETTINGS = EconomicsSettings(
    version=1,
    tax_percent=Decimal("6"), reserve_percent=Decimal("3"), rent_percent=Decimal("5"),
    manager_percent=Decimal("3"), marketing_percent=Decimal("5"), measure_percent=Decimal("3"),
    owner_ae_share_percent=Decimal("40"), partner_installation_share_percent=Decimal("50"),
)


class FunnelAndEconomicsTests(unittest.TestCase):
    def test_legacy_and_new_stages_have_one_semantic_mapping(self):
        self.assertEqual(semantic_funnel_stage("proposal"), "proposal_sent")
        self.assertEqual(semantic_funnel_stage("negotiation"), "decision_pending")
        self.assertEqual(semantic_funnel_stage("installed"), "installed")

    def test_calculates_reproducible_snapshot_values(self):
        result = calculate_economics(EconomicsInputs(
            quoted_price=Decimal("100000"), materials_cost=Decimal("20000"),
            production_cost=Decimal("10000"), installation_direct_cost=Decimal("10000"),
            installation_mode="solo",
        ), SETTINGS)
        self.assertEqual(result.director_profit, Decimal("35000.00"))
        self.assertEqual(result.ae_amount, Decimal("14000.00"))
        self.assertEqual(result.ae_percent, Decimal("0.14000000"))
        self.assertEqual(result.price_floor_ae_8, Decimal("72727.27"))
        self.assertEqual(result.price_floor_ae_10, Decimal("80000.00"))
        self.assertEqual(result.price_floor_ae_12, Decimal("88888.89"))
        self.assertEqual(result.owner_income_solo, Decimal("30000.00"))
        self.assertEqual(result.owner_income_with_partner, Decimal("25000.00"))
        self.assertEqual(result.projected_owner_income, Decimal("30000.00"))
        self.assertEqual(result.economics_status, "golden")

    def test_invalid_floor_is_not_silent_failure(self):
        impossible = EconomicsSettings(
            version=2, tax_percent=Decimal("60"), reserve_percent=Decimal("20"),
            rent_percent=Decimal("10"), manager_percent=Decimal("10"), marketing_percent=Decimal("5"),
            measure_percent=Decimal("5"), owner_ae_share_percent=Decimal("40"),
        )
        result = calculate_economics(EconomicsInputs(quoted_price=Decimal("100000")), impossible)
        self.assertEqual(result.economics_status, "rebuild_or_reject")
        self.assertIsNotNone(result.calculation_error)
        self.assertIsNone(result.price_floor_ae_8)

    def test_safe_cash_keeps_settled_cost_as_realized_outflow(self):
        totals = calculate_safe_cash(
            [
                {"kind": "customer_incoming", "amount": "200"},
                {"kind": "customer_refund", "amount": "25"},
                {"kind": "realized_cost_outflow", "amount": "80"},
                {"kind": "other_reserved_cash", "amount": "20"},
                {"kind": "other_reserved_cash_release", "amount": "5"},
            ],
            [{"status": "open", "amount": "40"}, {"status": "settled", "amount": "30"}],
        )
        self.assertEqual(totals.net_confirmed_customer_cash, Decimal("175.00"))
        self.assertEqual(totals.realized_cost_outflows, Decimal("80.00"))
        self.assertEqual(totals.open_reserved_obligations, Decimal("40.00"))
        self.assertEqual(totals.other_reserved_cash, Decimal("15.00"))
        self.assertEqual(totals.safe_cash, Decimal("40.00"))

    def test_compensating_movement_corrects_balance_without_changing_original(self):
        movements = [
            {"kind": "customer_incoming", "amount": "150000.00"},
            {"kind": "customer_refund", "amount": "150000.00", "reversal_of_movement_id": "original"},
        ]
        snapshot = [dict(row) for row in movements]
        totals = calculate_safe_cash(movements, [])
        self.assertEqual(totals.net_confirmed_customer_cash, Decimal("0.00"))
        self.assertEqual(totals.safe_cash, Decimal("0.00"))
        self.assertEqual(movements, snapshot)


class SettlementCursor:
    """Records the two immutable writes made by an obligation settlement."""

    def __init__(self):
        self.obligation_id = uuid4()
        self.deal_id = uuid4()
        self.movement_id = uuid4()
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.queries.append((sql, params))

    def fetchone(self):
        query = self.queries[-1][0]
        if "SELECT id,deal_id,amount,status" in query:
            return (self.obligation_id, self.deal_id, Decimal("60000.00"), "open")
        if "INSERT INTO deal_cash_movements" in query:
            return (self.movement_id,)
        raise AssertionError(query)


class CashflowSettlementTests(unittest.TestCase):
    def test_settlement_posts_realized_cost_then_removes_only_open_reservation(self):
        cursor = SettlementCursor()
        movement_id = settle_cost_obligation(
            cursor, cursor.obligation_id, occurred_at="occurred", confirmed_at="confirmed", actor_id=uuid4()
        )
        self.assertEqual(movement_id, cursor.movement_id)
        insert = next((params for sql, params in cursor.queries if "INSERT INTO deal_cash_movements" in sql), None)
        update = next((params for sql, params in cursor.queries if "UPDATE deal_cost_obligations" in sql), None)
        self.assertIsNotNone(insert)
        self.assertEqual(insert[0], cursor.deal_id)
        self.assertEqual(insert[1], cursor.obligation_id)
        self.assertEqual(insert[2], Decimal("60000.00"))
        self.assertEqual(update[1], cursor.movement_id)
        self.assertEqual(update[2], cursor.obligation_id)


class RecognitionCursor:
    """Tiny cursor model proving recognition is attempted only once at installed."""

    def __init__(self):
        self.deal_id = uuid4()
        self.current_revision = uuid4()
        self.inserted = False
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.queries.append((sql, params))

    def fetchone(self):
        query = self.queries[-1][0]
        if "FROM deals" in query:
            return (self.deal_id, self.current_revision)
        if "FROM economics_settings_versions" in query:
            return (1, "installed")
        if "FROM deal_economics_revisions" in query:
            return (Decimal("25000.00"),)
        if "INSERT INTO deal_income_recognitions" in query:
            if self.inserted:
                return None
            self.inserted = True
            return (uuid4(),)
        raise AssertionError(query)


class IncomeRecognitionTests(unittest.TestCase):
    def test_default_recognition_is_installed_and_idempotent(self):
        cursor = RecognitionCursor()
        self.assertFalse(recognize_income_for_stage(cursor, cursor.deal_id, "closed_won"))
        self.assertTrue(recognize_income_for_stage(cursor, cursor.deal_id, "installed"))
        self.assertFalse(recognize_income_for_stage(cursor, cursor.deal_id, "installed"))
        insert_params = [params for sql, params in cursor.queries if "INSERT INTO deal_income_recognitions" in sql]
        self.assertEqual(len(insert_params), 2)
        self.assertEqual(insert_params[0][3], "installed")
        self.assertEqual(insert_params[0][4], Decimal("25000.00"))


if __name__ == "__main__":
    unittest.main()
