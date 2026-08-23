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

    def test_duplicate_removal_requires_an_explicit_confirmation(self):
        self.assertEqual(app.DealDelete(confirmation="DELETE").confirmation, "DELETE")
        with self.assertRaises(app.ValidationError):
            app.DealDelete(confirmation="delete")


class DealNavigationPolicyTests(unittest.TestCase):
    def test_pipeline_cards_receive_semantic_stage_from_the_server(self):
        user = app.User(id=uuid4(), email="owner@example.test", display_name="Owner", role="manager")
        rows = [{
            "id": uuid4(), "title": "Legacy proposal", "source_stage": "proposal",
            "qualification_segment": "over_80k", "commercial_value": 75000,
            "projected_owner_income": 12000, "contact_name": "Client", "phone_normalized": "+79990000000",
        }]
        with patch.object(app, "fetch_all", return_value=rows):
            result = app.pipeline_deals(user)
        self.assertEqual(result[0]["stage"], "proposal_sent")
        self.assertNotIn("source_stage", result[0])

    def test_confirmed_cash_movements_still_have_no_mutating_route(self):
        cash_routes = {
            (route.path, tuple(sorted(route.methods or [])))
            for route in app.app.routes if "cash-movements" in route.path
        }
        self.assertNotIn(("/api/deals/{deal_id}/cash-movements/{movement_id}", ("DELETE",)), cash_routes)
        self.assertNotIn(("/api/deals/{deal_id}/cash-movements/{movement_id}", ("PATCH",)), cash_routes)


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


class ManualContactPhonePolicyTests(unittest.TestCase):
    def test_selected_contact_phone_must_belong_to_a_contact_and_cannot_be_combined_with_raw_override(self):
        contact_id, phone_id = uuid4(), uuid4()
        payload = app.CallInitiate(contact_id=contact_id, contact_phone_number_id=phone_id)
        self.assertEqual(payload.contact_id, contact_id)
        self.assertEqual(payload.contact_phone_number_id, phone_id)
        with self.assertRaises(app.ValidationError):
            app.CallInitiate(contact_phone_number_id=phone_id)
        with self.assertRaises(app.ValidationError):
            app.CallInitiate(contact_id=contact_id, contact_phone_number_id=phone_id, phone="79991112233")

    def test_contact_phone_rows_have_create_and_metadata_only_update_routes(self):
        routes = {
            (route.path, tuple(sorted(route.methods or [])))
            for route in app.app.routes if "phone-numbers" in route.path
        }
        self.assertIn(("/api/contacts/{contact_id}/phone-numbers", ("POST",)), routes)
        self.assertIn(("/api/contacts/{contact_id}/phone-numbers/{phone_number_id}", ("PATCH",)), routes)
        self.assertNotIn(("/api/contacts/{contact_id}/phone-numbers/{phone_number_id}", ("DELETE",)), routes)

    def test_manual_contact_and_phone_models_trim_human_labels(self):
        contact = app.ContactCreate(full_name="  Анна  ", phone="8 999 111-22-33", email="  anna@example.test  ")
        phone = app.ContactPhoneNumberCreate(phone="8 999 111-22-34", label="  Помощник  ", role="assistant")
        self.assertEqual(contact.full_name, "Анна")
        self.assertEqual(contact.email, "anna@example.test")
        self.assertEqual(phone.label, "Помощник")


def unknown_field(value=None):
    return {"proposed_value": value, "confidence": None, "evidence": [], "inference_status": "unknown"}


def deal_update_payload(*, pain="Новая боль", stage=None, next_contact=None, baseline_pain="Старая боль"):
    fields = {
        name: unknown_field([] if name in {"pain_secondary", "decision_makers"} else None)
        for name in app.AI_DEAL_UPDATE_FIELDS
    }
    fields["pain_primary"] = {
        "proposed_value": pain, "confidence": 0.92,
        "evidence": [{"segment_start_ms": 100, "segment_end_ms": 900, "quote": "Нам нужна защита от дождя"}],
        "inference_status": "supported",
    }
    if stage is not None:
        fields["suggested_stage"] = {
            "proposed_value": stage, "confidence": 0.9,
            "evidence": [{"segment_start_ms": 1000, "segment_end_ms": 1800, "quote": "Созвонимся позже"}],
            "inference_status": "supported",
        }
    if next_contact is not None:
        fields["next_contact_at"] = {
            "proposed_value": next_contact, "confidence": 0.9,
            "evidence": [{"segment_start_ms": 1000, "segment_end_ms": 1800, "quote": "Созвонимся завтра"}],
            "inference_status": "supported",
        }
    baseline = {"stage": "qualified", **{field: None for field in app.AI_DEAL_UPDATE_FIELDS if field != "suggested_stage"}}
    baseline["pain_primary"] = baseline_pain
    return {"proposed_fields": fields, "base_values": baseline}


class DraftCursor:
    def __init__(self, draft, target):
        self.draft = draft
        self.target = target
        self.current = None
        self.deal_update_count = 0
        self.audit_count = 0

    def execute(self, sql, params=()):
        if "FROM ai_action_drafts d" in sql:
            self.current = self.draft
        elif "FROM deals d JOIN calls" in sql:
            self.current = self.target
        elif sql.startswith("UPDATE deals SET"):
            self.deal_update_count += 1
            if "pain_primary=%s" in sql:
                self.target["pain_primary"] = params[0]
            if "stage=%s" in sql:
                self.target["stage"] = params[0]
            self.current = {"id": self.target["id"], "stage": self.target["stage"]}
        elif "UPDATE ai_action_drafts SET status='approved'" in sql:
            self.draft["status"] = "approved"
            self.current = self.draft
        elif "UPDATE ai_action_drafts SET status='rejected'" in sql:
            self.draft["status"] = "rejected"
            self.current = self.draft
        elif "INSERT INTO audit_log" in sql:
            self.audit_count += 1
            self.current = None
        elif "SELECT active FROM deal_reason_catalog" in sql:
            self.current = {"active": True}
        elif "FROM transcript_segments" in sql:
            self.current = None
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return [{"text": "Нам нужна защита от дождя. Созвонимся позже."}]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DraftConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DraftPool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        return self.connection_value


class DealUpdateDraftTests(unittest.TestCase):
    def setUp(self):
        self.user = app.User(id=uuid4(), email="owner@example.test", display_name="Owner", role="manager")
        self.deal_id = uuid4()
        self.draft_id = uuid4()

    def state(self, *, payload=None, target_owner=None, current_pain="Старая боль"):
        draft = {
            "id": self.draft_id, "call_id": 77, "contact_id": uuid4(), "owner_id": self.user.id,
            "kind": "deal_update", "status": "pending", "target_deal_id": self.deal_id,
            "payload": payload or deal_update_payload(),
        }
        target = {"id": self.deal_id, "contact_id": draft["contact_id"], "owner_id": target_owner or self.user.id,
                  "stage": "qualified", "pain_primary": current_pain}
        cursor = DraftCursor(draft, target)
        return draft, target, cursor, DraftConnection(cursor)

    def test_existing_deal_update_approves_once_without_creating_another_deal(self):
        draft, target, cursor, connection = self.state()
        with patch.object(app, "pool", DraftPool(connection)):
            result = app.decide_action_draft(self.draft_id, app.ActionDraftDecision(action="approve"), self.user)
            with self.assertRaises(HTTPException) as repeated:
                app.decide_action_draft(self.draft_id, app.ActionDraftDecision(action="approve"), self.user)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(target["pain_primary"], "Новая боль")
        self.assertEqual(cursor.deal_update_count, 1)
        self.assertEqual(repeated.exception.status_code, 409)
        self.assertEqual(cursor.audit_count, 1)
        self.assertEqual(connection.commits, 1)

    def test_stale_draft_does_not_overwrite_manager_change(self):
        _draft, target, cursor, connection = self.state(current_pain="Изменено менеджером")
        with patch.object(app, "pool", DraftPool(connection)), patch.object(app, "audit"):
            with self.assertRaises(HTTPException) as error:
                app.decide_action_draft(self.draft_id, app.ActionDraftDecision(action="approve"), self.user)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(target["pain_primary"], "Изменено менеджером")
        self.assertEqual(cursor.deal_update_count, 0)

    def test_rbac_denies_ai_update_to_foreign_target_deal(self):
        _draft, _target, _cursor, connection = self.state(target_owner=uuid4())
        with patch.object(app, "pool", DraftPool(connection)), patch.object(app, "audit"):
            with self.assertRaises(HTTPException) as error:
                app.decide_action_draft(self.draft_id, app.ActionDraftDecision(action="approve"), self.user)
        self.assertEqual(error.exception.status_code, 404)

    def test_ai_stage_suggestion_uses_normal_transition_validation(self):
        payload = deal_update_payload(stage="decision_pending")
        _draft, _target, cursor, connection = self.state(payload=payload)
        with patch.object(app, "pool", DraftPool(connection)), patch.object(app, "audit"):
            with self.assertRaises(HTTPException) as error:
                app.decide_action_draft(self.draft_id, app.ActionDraftDecision(action="approve"), self.user)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(cursor.deal_update_count, 0)

    def test_preview_shows_current_proposed_confidence_and_evidence(self):
        draft, target, _cursor, _connection = self.state()
        with patch.object(app, "fetch_one", side_effect=[draft, target]):
            preview = app.action_draft_preview(self.draft_id, self.user)
        self.assertFalse(preview["stale"])
        self.assertEqual(preview["diff"][0]["field"], "pain_primary")
        self.assertEqual(preview["diff"][0]["current_value"], "Старая боль")
        self.assertEqual(preview["diff"][0]["proposed_value"], "Новая боль")
        self.assertEqual(preview["diff"][0]["evidence"][0]["segment_start_ms"], 100)

    def test_ai_payload_cannot_include_commercial_price_or_cashflow_fields(self):
        payload = deal_update_payload()
        payload["proposed_fields"]["quoted_price"] = {
            "proposed_value": 1, "confidence": 1,
            "evidence": [{"segment_start_ms": 1, "segment_end_ms": 2, "quote": "Нельзя"}],
            "inference_status": "supported",
        }
        values, _fields, _baseline = app.ai_deal_update_values(payload)
        self.assertEqual(set(values), {"pain_primary"})


class ReasonCatalogPolicyTests(unittest.TestCase):
    def test_disabled_reason_cannot_be_selected_for_new_transition(self):
        class Cursor:
            def execute(self, *_args):
                pass

            def fetchone(self):
                return {"active": False}

        before = {"stage": "proposal", "loss_reason": None, "disqualification_reason": None}
        with self.assertRaises(HTTPException) as error:
            app.validate_transition_reason_catalog(Cursor(), before, {"stage": "closed_lost", "loss_reason": "old_reason"})
        self.assertEqual(error.exception.status_code, 422)

    def test_only_admin_can_modify_reason_catalog(self):
        manager = app.User(id=uuid4(), email="manager@example.test", display_name="Manager", role="manager")
        with self.assertRaises(HTTPException) as error:
            app.require_admin(manager)
        self.assertEqual(error.exception.status_code, 403)

    def test_admin_catalog_create_and_disable_are_audited(self):
        admin = app.User(id=uuid4(), email="admin@example.test", display_name="Admin", role="admin")
        reason_id = uuid4()
        created = {"id": reason_id, "kind": "lost", "code": "other_price", "label": "Другая цена", "active": True}
        disabled = {**created, "active": False}
        with patch.object(app, "execute", side_effect=[created, disabled]), patch.object(
            app, "fetch_one", return_value=created
        ), patch.object(app, "audit") as audit:
            app.create_deal_reason(app.ReasonCatalogCreate(kind="lost", code="other_price", label="Другая цена"), admin)
            app.update_deal_reason(reason_id, app.ReasonCatalogPatch(active=False), admin)
        self.assertEqual(audit.call_count, 2)
        self.assertEqual(audit.call_args_list[1].args[3], "disable")

    def test_disabled_reason_remains_in_historical_deal_detail(self):
        user = app.User(id=uuid4(), email="owner@example.test", display_name="Owner", role="manager")
        deal = {"id": uuid4(), "owner_id": user.id, "loss_reason": "old_reason", "disqualification_reason": None}
        disabled = {"kind": "lost", "code": "old_reason", "label": "Старая причина", "active": False}
        with patch.object(app, "fetch_one", side_effect=[deal, disabled]), patch.object(app, "fetch_all", return_value=[]):
            result = app.deal_detail(deal["id"], user)
        self.assertFalse(result["loss_reason_catalog"]["active"])


class AIDraftWorkspaceMarkupTests(unittest.TestCase):
    def test_call_and_admin_workspaces_expose_preview_decision_and_catalog_controls(self):
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        for required in (
            '/preview', 'draft-diff', 'data-action="draft-decision"', 'Применить черновик',
            'Отклонить', '/api/admin/deal-reasons', 'reason-create-form', 'data-reason-form=',
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
