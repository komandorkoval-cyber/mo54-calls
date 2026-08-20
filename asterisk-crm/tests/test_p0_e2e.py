"""Final P0 acceptance flows against an isolated disposable PostgreSQL database.

These tests deliberately exercise the worker persistence path and HTTP API
together.  They are not a production deployment test and never address a
shared database or any MO54 resource.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
WORKER = ROOT / "worker"
sys.path.insert(0, str(Path(__file__).parent))

from test_p0_database import BASE, P0_010, P0_AFTER_010, P0_BEFORE_010, PostgresHarness  # noqa: E402


class NetworkedPostgresHarness(PostgresHarness):
    """A disposable PostgreSQL container reachable only through loopback."""

    def start(self):
        subprocess.run([
            "docker", "run", "--rm", "-d", "--name", self.container,
            "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD=test", "postgres:16-alpine",
        ], check=True, capture_output=True, text=True)
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", self.container, "pg_isready", "-U", "postgres"],
                                   capture_output=True, text=True)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        binding = subprocess.run(["docker", "port", self.container, "5432/tcp"],
                                 check=True, capture_output=True, text=True).stdout.splitlines()[0]
        self.host_port = int(binding.rsplit(":", 1)[1])


def load_worker_database_module(database_url: str):
    """Load only the worker persistence module through a psycopg3 test shim.

    The production worker uses psycopg2.  The API test dependency set supplies
    psycopg3, so this thin, local shim keeps the acceptance test dependency-free
    while running the real worker ``save_transcript`` and ``save_insight`` code.
    """

    from psycopg import connect

    previous_psycopg2 = sys.modules.get("psycopg2")
    previous_extras = sys.modules.get("psycopg2.extras")
    previous_novofon = sys.modules.get("novofon")
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_psycopg2.connect = lambda dsn: connect(dsn)
    fake_extras.Json = lambda value: json.dumps(value, ensure_ascii=False)
    fake_extras.RealDictCursor = object
    fake_psycopg2.extras = fake_extras
    sys.modules["psycopg2"] = fake_psycopg2
    sys.modules["psycopg2.extras"] = fake_extras

    worker_novofon_spec = importlib.util.spec_from_file_location("p0_e2e_worker_novofon", WORKER / "novofon.py")
    assert worker_novofon_spec and worker_novofon_spec.loader
    worker_novofon = importlib.util.module_from_spec(worker_novofon_spec)
    sys.modules[worker_novofon_spec.name] = worker_novofon
    worker_novofon_spec.loader.exec_module(worker_novofon)
    sys.modules["novofon"] = worker_novofon
    worker_db_spec = importlib.util.spec_from_file_location("p0_e2e_worker_db", WORKER / "db.py")
    assert worker_db_spec and worker_db_spec.loader
    worker_db = importlib.util.module_from_spec(worker_db_spec)
    sys.modules[worker_db_spec.name] = worker_db
    try:
        worker_db_spec.loader.exec_module(worker_db)
    finally:
        if previous_novofon is None:
            sys.modules.pop("novofon", None)
        else:
            sys.modules["novofon"] = previous_novofon
        if previous_psycopg2 is None:
            sys.modules.pop("psycopg2", None)
        else:
            sys.modules["psycopg2"] = previous_psycopg2
        if previous_extras is None:
            sys.modules.pop("psycopg2.extras", None)
        else:
            sys.modules["psycopg2.extras"] = previous_extras
    assert worker_db.DATABASE_URL == database_url
    return worker_db


class P0EndToEndAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_cwd = Path.cwd()
        cls.pg = NetworkedPostgresHarness()
        cls.pg.start()
        cls.database = f"p0_e2e_{uuid.uuid4().hex[:12]}"
        cls.pg.create_database(cls.database)
        for migration in [*BASE, *P0_BEFORE_010, P0_010, *P0_AFTER_010]:
            cls.pg.execute_file(cls.database, migration)

        cls.database_url = f"postgresql://postgres:test@127.0.0.1:{cls.pg.host_port}/{cls.database}"
        os.environ["DATABASE_URL"] = cls.database_url
        # This is a process-local disposable-database value.  No .env file or
        # production credential is read or written by these tests.
        os.environ["CRM_ADMIN_PASSWORD"] = "p0-e2e-only-password"
        cls.worker_db = load_worker_database_module(cls.database_url)
        os.chdir(API)
        sys.path.insert(0, str(API))
        cls.app = importlib.import_module("app")
        cls.app.pool.open()
        from fastapi.testclient import TestClient
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.pool.close()
        finally:
            os.chdir(cls.original_cwd)
            cls.pg.stop()

    def setUp(self):
        # Contacts own the P0 deal/call graph.  Settings and reason catalogues
        # remain as migration-seeded configuration shared by independent tests.
        self.db("TRUNCATE TABLE audit_log, contacts CASCADE")
        self.owner = self.create_user("manager")
        self.admin = self.create_user("admin")
        self.other = self.create_user("manager")

    def db(self, sql: str, params: tuple = (), *, many: bool = False):
        with self.app.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            result = list(cur.fetchall()) if many else (cur.fetchone() if cur.description else None)
            conn.commit()
            return result

    def scalar(self, sql: str, params: tuple = ()):  # Explicitly accept dict-row scalar projections.
        row = self.db(sql, params)
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

    def create_user(self, role: str):
        email = f"{role}-{uuid.uuid4().hex}@p0-e2e.test"
        row = self.db(
            """INSERT INTO crm_users(email,display_name,password_hash,role,must_change_password)
               VALUES(%s,%s,'not-used-in-e2e',%s,false)
               RETURNING id,email,display_name,role::text AS role,must_change_password""",
            (email, role.title(), role),
        )
        return self.app.User(**row)

    def client_for(self, user):
        token, _ = self.app.new_server_session(user.id)
        client = self.TestClient(self.app.app, raise_server_exceptions=True)
        headers = {
            "Origin": "http://testserver",
            "Cookie": f"{self.app.SESSION_COOKIE_NAME}={token}",
        }
        return client, headers

    def create_contact_and_deal(self, *, owner=None, stage="qualified", qualification="unknown", quoted_price=None):
        owner = owner or self.owner
        contact = self.db(
            "INSERT INTO contacts(full_name,phone_normalized) VALUES(%s,%s) RETURNING *",
            ("Acceptance client", f"+7999{uuid.uuid4().int % 10**7:07d}"),
        )
        deal = self.db(
            """INSERT INTO deals(contact_id,owner_id,title,stage,qualification_segment,quoted_price)
               VALUES(%s,%s,'Terrace acceptance',%s,%s,%s) RETURNING *,stage::text AS stage""",
            (contact["id"], owner.id, stage, qualification, quoted_price),
        )
        return contact, deal

    def create_call(self, contact_id, owner_id, *, deal_id=None):
        marker = uuid.uuid4().hex
        call = self.db(
            """INSERT INTO calls(call_id,source,external_call_id,direction,caller_number,callee_number,
                                 started_at,duration_sec,contact_id,owner_id,status,processing_status)
               VALUES(%s,'asterisk',%s,'in','+79990000000','+79991111111',now(),120,%s,%s,'new','recorded')
               RETURNING *""",
            (marker, marker, contact_id, owner_id),
        )
        if deal_id:
            self.db("INSERT INTO call_deals(call_id,deal_id) VALUES(%s,%s)", (call["id"], deal_id))
        return call

    @staticmethod
    def unknown(value=None):
        return {"proposed_value": value, "confidence": None, "evidence": [], "inference_status": "unknown"}

    def proposal(self, *, supported):
        fields = {
            name: self.unknown([] if name in {"pain_secondary", "decision_makers"} else None)
            for name in self.worker_db.AI_DEAL_UPDATE_FIELDS
        }
        evidence = {"segment_start_ms": 0, "segment_end_ms": 5000,
                    "quote": "Budget 120000, Elena decides, installation in September, call tomorrow."}
        for name, value in supported.items():
            fields[name] = {"proposed_value": value, "confidence": 0.94,
                            "evidence": [dict(evidence)], "inference_status": "supported"}
        return fields

    def save_update_draft(self, deal, *, fields=None):
        call = self.create_call(deal["contact_id"], deal["owner_id"], deal_id=deal["id"])
        transcript = "Budget 120000, Elena decides, installation in September, call tomorrow."
        self.worker_db.save_transcript(
            call["id"], transcript,
            [{"speaker": "customer", "started_ms": 0, "ended_ms": 5000, "text": transcript}], "e2e-asr",
        )
        fields = fields or self.proposal(supported={"pain_primary": "Rain protection"})
        insight = {
            "summary": "Acceptance", "customer_intent": None, "customer_need": None, "product": "Terrace",
            "budget_amount": None, "timeline": None, "decision_maker": None, "lead_stage": "qualified",
            "lead_temperature": "warm", "objections": [], "manager_responses": [], "agreements": [],
            "customer_promises": [], "company_promises": [], "next_step": None, "next_step_date": None,
            "next_step_owner": None, "loss_risk": "low", "outcome": "continue_work",
            "quality_scores": {"discovery": 4, "clarity": 4, "objection_handling": 4, "next_step": 4},
            "recommendations": [], "evidence": [], "confidence": 0.94,
            "commercial_proposal": {"fields": fields},
        }
        self.worker_db.save_insight(call["id"], insight, "e2e-llm", "sales-v1.1")
        return call, self.db("SELECT * FROM ai_action_drafts WHERE call_id=%s AND kind='deal_update'", (call["id"],))

    def test_existing_deal_ai_update_is_reviewable_applied_once_and_audited(self):
        _contact, deal = self.create_contact_and_deal(qualification="under_80k", quoted_price=75000)
        fields = self.proposal(supported={
            "qualification_segment": "over_80k", "estimated_budget_min": 120000,
            "estimated_budget_max": 140000, "budget_range": "120_160", "pain_primary": "Rain protection",
            "decision_makers": ["Elena"], "decision_maker_status": "single",
            "desired_install_period": "September", "next_contact_at": "2026-09-02T10:00:00+07:00",
            "suggested_stage": "decision_pending",
        })
        _call, draft = self.save_update_draft(deal, fields=fields)
        client, headers = self.client_for(self.owner)
        try:
            preview = client.get(f"/api/action-drafts/{draft['id']}/preview", headers=headers)
            self.assertEqual(preview.status_code, 200, preview.text)
            diff = {item["field"]: item for item in preview.json()["diff"]}
            self.assertEqual(diff["qualification_segment"]["current_value"], "under_80k")
            self.assertEqual(diff["qualification_segment"]["proposed_value"], "over_80k")
            self.assertEqual(diff["pain_primary"]["confidence"], 0.94)
            evidence = diff["pain_primary"]["evidence"][0]
            self.assertEqual((evidence["segment_start_ms"], evidence["segment_end_ms"]), (0, 5000))
            transcript_segment = self.db(
                """SELECT s.text FROM transcript_segments s JOIN transcripts t ON t.id=s.transcript_id
                   WHERE t.call_id=%s AND s.started_ms=0 AND s.ended_ms=5000""",
                (_call["id"],),
            )
            self.assertIn(evidence["quote"], transcript_segment["text"])

            approved = client.post(f"/api/action-drafts/{draft['id']}/decision", headers=headers,
                                   json={"action": "approve"})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "approved")
            detail = client.get(f"/api/deals/{deal['id']}", headers=headers)
            self.assertEqual(detail.status_code, 200, detail.text)
            changed = detail.json()
            self.assertEqual(changed["qualification_segment"], "over_80k")
            self.assertEqual(float(changed["estimated_budget_min"]), 120000)
            self.assertEqual(float(changed["estimated_budget_max"]), 140000)
            self.assertEqual(changed["pain_primary"], "Rain protection")
            self.assertEqual(changed["decision_makers"], ["Elena"])
            self.assertEqual(changed["desired_install_period"], "September")
            self.assertTrue(changed["next_contact_at"].startswith("2026-09-02T03:00:00"))
            self.assertEqual(changed["stage"], "decision_pending")
            self.assertIsNone(changed["alternative_considered"])

            replay = client.post(f"/api/action-drafts/{draft['id']}/decision", headers=headers,
                                 json={"action": "approve"})
            self.assertEqual(replay.status_code, 409)
        finally:
            client.close()
        self.assertEqual(self.scalar("SELECT count(*) FROM deals"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM audit_log WHERE entity_type='ai_action_draft' AND action='approve'"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ai_action_drafts WHERE id=%s AND status='approved'", (draft["id"],)), 1)

    def test_unsupported_ai_fact_is_not_materialized_and_stale_or_foreign_approval_is_blocked(self):
        _contact, deal = self.create_contact_and_deal()
        _call, stale_draft = self.save_update_draft(deal)
        owner_client, owner_headers = self.client_for(self.owner)
        try:
            manual = owner_client.patch(f"/api/deals/{deal['id']}", headers=owner_headers,
                                        json={"pain_primary": "Manager correction"})
            self.assertEqual(manual.status_code, 200, manual.text)
            stale = owner_client.post(f"/api/action-drafts/{stale_draft['id']}/decision", headers=owner_headers,
                                      json={"action": "approve"})
            self.assertEqual(stale.status_code, 409)
        finally:
            owner_client.close()
        self.assertEqual(self.scalar("SELECT pain_primary FROM deals WHERE id=%s", (deal["id"],)), "Manager correction")
        self.assertEqual(self.scalar("SELECT status FROM ai_action_drafts WHERE id=%s", (stale_draft["id"],)), "pending")

        _call, foreign_draft = self.save_update_draft(deal)
        other_client, other_headers = self.client_for(self.other)
        try:
            forbidden = other_client.post(f"/api/action-drafts/{foreign_draft['id']}/decision", headers=other_headers,
                                          json={"action": "approve"})
            self.assertEqual(forbidden.status_code, 403)
        finally:
            other_client.close()
        self.assertEqual(self.scalar("SELECT status FROM ai_action_drafts WHERE id=%s", (foreign_draft["id"],)), "pending")

        _call, unsafe_draft = self.save_update_draft(deal)
        self.db(
            """UPDATE ai_action_drafts
               SET payload=jsonb_set(payload, '{proposed_fields,pain_primary,evidence}', %s::jsonb)
               WHERE id=%s""",
            (json.dumps([{"segment_start_ms": 0, "segment_end_ms": 5000, "quote": "invented fact"}]), unsafe_draft["id"]),
        )
        owner_client, owner_headers = self.client_for(self.owner)
        try:
            unsafe = owner_client.post(f"/api/action-drafts/{unsafe_draft['id']}/decision", headers=owner_headers,
                                       json={"action": "approve"})
            self.assertEqual(unsafe.status_code, 422)
        finally:
            owner_client.close()
        self.assertEqual(self.scalar("SELECT status FROM ai_action_drafts WHERE id=%s", (unsafe_draft["id"],)), "pending")
        self.assertEqual(self.scalar("SELECT pain_primary FROM deals WHERE id=%s", (deal["id"],)), "Manager correction")

    def test_economics_revisions_floor_override_and_installed_recognition_are_immutable(self):
        _contact, deal = self.create_contact_and_deal(stage="qualified")
        client, headers = self.client_for(self.owner)
        try:
            first = client.post(f"/api/deals/{deal['id']}/economics/revisions", headers=headers, json={
                "quoted_price": 160000, "materials_cost": 45000, "production_cost": 15000,
                "installation_direct_cost": 10000, "installation_mode": "solo",
            })
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post(f"/api/deals/{deal['id']}/economics/revisions", headers=headers, json={
                "quoted_price": 180000, "materials_cost": 45000, "production_cost": 15000,
                "installation_direct_cost": 10000, "installation_mode": "solo",
            })
            self.assertEqual(second.status_code, 200, second.text)
            rev1, rev2 = first.json(), second.json()
            self.assertEqual((rev1["revision"], rev2["revision"]), (1, 2))
            self.assertEqual(rev1["settings_version"], rev2["settings_version"])
            self.assertEqual(float(rev1["quoted_price"]), 160000)
            self.assertEqual(self.scalar("SELECT current_economics_revision_id::text FROM deals WHERE id=%s", (deal["id"],)), rev2["id"])

            revision_blocked = client.post(f"/api/deals/{deal['id']}/economics/revisions", headers=headers, json={
                "quoted_price": 100000, "materials_cost": 45000, "production_cost": 15000,
                "installation_direct_cost": 10000, "installation_mode": "solo",
            })
            self.assertEqual(revision_blocked.status_code, 422)
            blocked = client.patch(f"/api/deals/{deal['id']}", headers=headers, json={"quoted_price": 100000})
            self.assertEqual(blocked.status_code, 422)
            overridden = client.patch(f"/api/deals/{deal['id']}", headers=headers, json={
                "quoted_price": 100000, "price_floor_override_reason": "approved acceptance exception",
            })
            self.assertEqual(overridden.status_code, 200, overridden.text)
            audit = self.db("""SELECT before_data,after_data FROM audit_log WHERE entity_type='deal' AND action='update'
                              ORDER BY id DESC LIMIT 1""")
            self.assertEqual(float(audit["before_data"]["quoted_price"]), 180000)
            self.assertEqual(float(audit["after_data"]["quoted_price"]), 100000)
            self.assertEqual(audit["after_data"]["price_floor_override_reason"], "approved acceptance exception")
            self.assertIsNotNone(audit["after_data"]["price_floor_ae_8"])
            self.assertEqual(float(audit["after_data"]["ae_before"]["percent"]), float(rev2["ae_percent"]))
            self.assertNotEqual(float(audit["after_data"]["ae_after"]["percent"]), float(rev2["ae_percent"]))
            override_revision = self.scalar("SELECT current_economics_revision_id::text FROM deals WHERE id=%s", (deal["id"],))
            self.assertNotEqual(override_revision, rev2["id"])

            closed_won = client.patch(f"/api/deals/{deal['id']}", headers=headers, json={"stage": "closed_won"})
            self.assertEqual(closed_won.status_code, 200, closed_won.text)
            self.assertEqual(self.scalar("SELECT count(*) FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],)), 0)
            installed = client.patch(f"/api/deals/{deal['id']}", headers=headers, json={"stage": "installed"})
            self.assertEqual(installed.status_code, 200, installed.text)
            recognition = self.db("SELECT * FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],))
            override_projected = self.scalar("SELECT projected_owner_income FROM deal_economics_revisions WHERE id=%s", (override_revision,))
            self.assertEqual(str(recognition["economics_revision_id"]), override_revision)
            self.assertEqual(float(recognition["recognized_owner_income"]), float(override_projected))
            repeated = client.patch(f"/api/deals/{deal['id']}", headers=headers, json={"stage": "installed"})
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(self.scalar("SELECT count(*) FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],)), 1)

            third = client.post(f"/api/deals/{deal['id']}/economics/revisions", headers=headers, json={
                "quoted_price": 220000, "materials_cost": 45000, "production_cost": 15000,
                "installation_direct_cost": 10000, "installation_mode": "solo",
            })
            self.assertEqual(third.status_code, 200, third.text)
            dashboard = client.get("/api/dashboard/season", headers=headers)
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            self.assertEqual(float(dashboard.json()["goal_owner_income"]), 800000)
            self.assertEqual(float(dashboard.json()["earned_owner_income"]), float(override_projected))
            self.assertEqual(float(dashboard.json()["projected_owner_income"]), float(third.json()["projected_owner_income"]))
            self.assertEqual(float(dashboard.json()["remaining_to_goal"]), 800000 - float(override_projected))
        finally:
            client.close()
        recognition_after = self.db("SELECT * FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],))
        self.assertEqual(str(recognition_after["economics_revision_id"]), override_revision)
        self.assertEqual(float(recognition_after["recognized_owner_income"]), float(override_projected))
        with self.assertRaises(Exception):
            self.db("UPDATE deal_economics_revisions SET quoted_price=2 WHERE id=%s", (rev1["id"],))

    def test_cashflow_lifecycle_keeps_safe_cash_and_uses_compensating_reversal(self):
        _contact, deal = self.create_contact_and_deal()
        client, headers = self.client_for(self.owner)
        try:
            payment = client.post(f"/api/deals/{deal['id']}/cash-movements", headers=headers,
                                  json={"kind": "customer_incoming", "amount": 150000, "note": "advance"})
            self.assertEqual(payment.status_code, 200, payment.text)
            obligation = client.post(f"/api/deals/{deal['id']}/cost-obligations", headers=headers,
                                     json={"amount": 50000, "description": "fabric"})
            self.assertEqual(obligation.status_code, 200, obligation.text)
            edited = client.patch(f"/api/deals/{deal['id']}/cost-obligations/{obligation.json()['id']}", headers=headers,
                                  json={"amount": 60000, "description": "fabric confirmed"})
            self.assertEqual(edited.status_code, 200, edited.text)
            before = client.get(f"/api/deals/{deal['id']}/cashflow", headers=headers)
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(float(before.json()["open_obligations"]), 60000)
            self.assertEqual(float(before.json()["safe_cash"]), 90000)

            settled = client.post(f"/api/deals/{deal['id']}/cost-obligations/{obligation.json()['id']}/settle", headers=headers)
            self.assertEqual(settled.status_code, 200, settled.text)
            after = client.get(f"/api/deals/{deal['id']}/cashflow", headers=headers)
            self.assertEqual(after.status_code, 200, after.text)
            self.assertEqual(float(after.json()["realized_costs"]), 60000)
            self.assertEqual(float(after.json()["open_obligations"]), 0)
            self.assertEqual(float(after.json()["safe_cash"]), 90000)
            obligation_row = next(row for row in after.json()["obligations"] if row["id"] == obligation.json()["id"])
            self.assertEqual(obligation_row["status"], "settled")
            self.assertEqual(obligation_row["settled_movement_id"], settled.json()["id"])

            refund = client.post(f"/api/deals/{deal['id']}/cash-movements", headers=headers,
                                 json={"kind": "customer_refund", "amount": 20000, "note": "partial refund"})
            self.assertEqual(refund.status_code, 200, refund.text)
            refunded = client.get(f"/api/deals/{deal['id']}/cashflow", headers=headers).json()
            self.assertEqual(float(refunded["net_confirmed_customer_cash"]), 130000)
            self.assertEqual(float(refunded["safe_cash"]), 70000)

            reversed_refund = client.post(f"/api/deals/{deal['id']}/cash-movements/{refund.json()['id']}/reverse", headers=headers,
                                           json={"reason": "refund cancelled"})
            self.assertEqual(reversed_refund.status_code, 200, reversed_refund.text)
            corrected = client.get(f"/api/deals/{deal['id']}/cashflow", headers=headers).json()
            self.assertEqual(float(corrected["net_confirmed_customer_cash"]), 150000)
            self.assertEqual(float(corrected["safe_cash"]), 90000)
        finally:
            client.close()
        self.assertEqual(self.scalar("SELECT amount FROM deal_cash_movements WHERE id=%s", (refund.json()["id"],)), 20000)
        self.assertEqual(self.scalar("SELECT count(*) FROM deal_cash_movements WHERE reversal_of_movement_id=%s", (refund.json()["id"],)), 1)
        with self.assertRaises(Exception):
            self.db("UPDATE deal_cash_movements SET amount=1 WHERE id=%s", (payment.json()["id"],))
        with self.assertRaises(Exception):
            self.db("DELETE FROM deal_cash_movements WHERE id=%s", (payment.json()["id"],))

    def test_configurable_recognition_stage_is_versioned_and_idempotent(self):
        _contact, deal = self.create_contact_and_deal(stage="qualified")
        admin_client, admin_headers = self.client_for(self.admin)
        owner_client, owner_headers = self.client_for(self.owner)
        settings_payload = {
            "tax_percent": 6, "reserve_percent": 3, "rent_percent": 5, "manager_percent": 3,
            "marketing_percent": 5, "measure_percent": 3, "owner_ae_share_percent": 40,
            "partner_installation_share_percent": 0, "golden_ae_percent": 10, "take_ae_percent": 8,
            "max_raise_price_delta_percent": 15,
        }
        try:
            configured = admin_client.post("/api/admin/economics-settings", headers=admin_headers,
                                           json={**settings_payload, "income_recognition_stage": "contract_signed"})
            self.assertEqual(configured.status_code, 200, configured.text)
            revision = owner_client.post(f"/api/deals/{deal['id']}/economics/revisions", headers=owner_headers, json={
                "quoted_price": 100000, "materials_cost": 30000, "installation_mode": "solo",
            })
            self.assertEqual(revision.status_code, 200, revision.text)
            self.assertEqual(revision.json()["settings_version"], configured.json()["version"])
            self.assertEqual(owner_client.patch(f"/api/deals/{deal['id']}", headers=owner_headers,
                                                json={"stage": "closed_won"}).status_code, 200)
            self.assertEqual(self.scalar("SELECT count(*) FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],)), 0)
            self.assertEqual(owner_client.patch(f"/api/deals/{deal['id']}", headers=owner_headers,
                                                json={"stage": "contract_signed"}).status_code, 200)
            recognition = self.db("SELECT * FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],))
            self.assertEqual(recognition["recognition_stage"], "contract_signed")
            self.assertEqual(recognition["settings_version"], configured.json()["version"])
            self.assertEqual(owner_client.patch(f"/api/deals/{deal['id']}", headers=owner_headers,
                                                json={"stage": "contract_signed"}).status_code, 200)
            self.assertEqual(self.scalar("SELECT count(*) FROM deal_income_recognitions WHERE deal_id=%s", (deal["id"],)), 1)
            reset = admin_client.post("/api/admin/economics-settings", headers=admin_headers,
                                      json={**settings_payload, "income_recognition_stage": "installed"})
            self.assertEqual(reset.status_code, 200, reset.text)
        finally:
            admin_client.close()
            owner_client.close()

    def test_semantic_funnel_segment_independence_reason_catalogues_and_rbac(self):
        contact_a, legacy = self.create_contact_and_deal(stage="proposal", qualification="over_80k", quoted_price=75000)
        contact_b, new = self.create_contact_and_deal(stage="measure_completed", qualification="under_80k", quoted_price=100000)
        owner_client, owner_headers = self.client_for(self.owner)
        admin_client, admin_headers = self.client_for(self.admin)
        other_client, other_headers = self.client_for(self.other)
        try:
            pipeline = owner_client.get("/api/pipeline", headers=owner_headers)
            self.assertEqual(pipeline.status_code, 200, pipeline.text)
            buckets = {(row["stage"], row["qualification_segment"]): row for row in pipeline.json()}
            self.assertIn(("proposal_sent", "over_80k"), buckets)
            self.assertIn(("measure_completed", "under_80k"), buckets)
            cards = owner_client.get("/api/pipeline/deals", headers=owner_headers)
            self.assertEqual(cards.status_code, 200, cards.text)
            legacy_card = next(row for row in cards.json() if row["id"] == str(legacy["id"]))
            self.assertEqual(legacy_card["stage"], "proposal_sent")
            self.assertNotIn("source_stage", legacy_card)
            self.assertEqual(self.scalar("SELECT qualification_segment FROM deals WHERE id=%s", (legacy["id"],)), "over_80k")
            self.assertEqual(self.scalar("SELECT qualification_segment FROM deals WHERE id=%s", (new["id"],)), "under_80k")

            admin_economics = admin_client.post(f"/api/deals/{new['id']}/economics/revisions", headers=admin_headers, json={
                "quoted_price": 100000, "materials_cost": 30000, "installation_mode": "solo",
            })
            self.assertEqual(admin_economics.status_code, 200, admin_economics.text)
            forbidden_economics = other_client.post(f"/api/deals/{new['id']}/economics/revisions", headers=other_headers, json={
                "quoted_price": 100000, "materials_cost": 30000, "installation_mode": "solo",
            })
            self.assertEqual(forbidden_economics.status_code, 404)

            reason = admin_client.post("/api/admin/deal-reasons", headers=admin_headers,
                                      json={"kind": "lost", "code": "acceptance_reason", "label": "Acceptance reason"})
            self.assertEqual(reason.status_code, 200, reason.text)
            transition = owner_client.patch(f"/api/deals/{legacy['id']}", headers=owner_headers,
                                            json={"stage": "closed_lost", "loss_reason": "acceptance_reason"})
            self.assertEqual(transition.status_code, 200, transition.text)
            disabled = admin_client.patch(f"/api/admin/deal-reasons/{reason.json()['id']}", headers=admin_headers,
                                          json={"active": False})
            self.assertEqual(disabled.status_code, 200, disabled.text)
            active = owner_client.get("/api/deal-reasons", headers=owner_headers)
            self.assertNotIn("acceptance_reason", {item["code"] for item in active.json()})
            historical = owner_client.get(f"/api/deals/{legacy['id']}", headers=owner_headers)
            self.assertEqual(historical.status_code, 200, historical.text)
            self.assertFalse(historical.json()["loss_reason_catalog"]["active"])
            forbidden_reason = other_client.post("/api/admin/deal-reasons", headers=other_headers,
                                                 json={"kind": "lost", "code": "forbidden", "label": "Forbidden"})
            self.assertEqual(forbidden_reason.status_code, 403)
            forbidden_deal = other_client.patch(f"/api/deals/{new['id']}", headers=other_headers,
                                                json={"pain_primary": "no"})
            self.assertEqual(forbidden_deal.status_code, 404)
            forbidden_cash = other_client.post(f"/api/deals/{new['id']}/cash-movements", headers=other_headers,
                                               json={"kind": "customer_incoming", "amount": 1})
            self.assertEqual(forbidden_cash.status_code, 404)
        finally:
            owner_client.close()
            admin_client.close()
            other_client.close()
        self.assertGreaterEqual(self.scalar("SELECT count(*) FROM audit_log WHERE entity_type='deal_reason_catalog'"), 2)

    def test_workspace_navigation_and_confirmed_duplicate_deletion(self):
        _contact, duplicate = self.create_contact_and_deal(stage="proposal", qualification="over_80k", quoted_price=75000)
        _protected_contact, protected = self.create_contact_and_deal()
        owner_client, owner_headers = self.client_for(self.owner)
        other_client, other_headers = self.client_for(self.other)
        try:
            listed = owner_client.get("/api/deals", headers=owner_headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertIn(str(duplicate["id"]), {row["id"] for row in listed.json()})
            card = next(row for row in owner_client.get("/api/pipeline/deals", headers=owner_headers).json()
                        if row["id"] == str(duplicate["id"]))
            self.assertEqual(card["stage"], "proposal_sent")

            missing_confirmation = owner_client.request("DELETE", f"/api/deals/{duplicate['id']}", headers=owner_headers, json={})
            self.assertEqual(missing_confirmation.status_code, 422)
            forbidden = other_client.request("DELETE", f"/api/deals/{duplicate['id']}", headers=other_headers,
                                             json={"confirmation": "DELETE"})
            self.assertEqual(forbidden.status_code, 404)
            deleted = owner_client.request("DELETE", f"/api/deals/{duplicate['id']}", headers=owner_headers,
                                           json={"confirmation": "DELETE"})
            self.assertEqual(deleted.status_code, 204, deleted.text)
            self.assertEqual(owner_client.get(f"/api/deals/{duplicate['id']}", headers=owner_headers).status_code, 404)
            deletion_audit = self.db(
                """SELECT before_data,after_data FROM audit_log
                   WHERE entity_type='deal' AND entity_id=%s AND action='delete'""",
                (str(duplicate["id"]),),
            )
            self.assertEqual(deletion_audit["after_data"], {"deleted": True, "confirmation": "DELETE"})
            self.assertEqual(deletion_audit["before_data"]["id"], str(duplicate["id"]))

            movement = owner_client.post(f"/api/deals/{protected['id']}/cash-movements", headers=owner_headers,
                                         json={"kind": "customer_incoming", "amount": 150000})
            self.assertEqual(movement.status_code, 200, movement.text)
            protected_delete = owner_client.request("DELETE", f"/api/deals/{protected['id']}", headers=owner_headers,
                                                    json={"confirmation": "DELETE"})
            self.assertEqual(protected_delete.status_code, 409)
            self.assertEqual(owner_client.get(f"/api/deals/{protected['id']}", headers=owner_headers).status_code, 200)
        finally:
            owner_client.close()
            other_client.close()


class P0MobileAndClientContractTests(unittest.TestCase):
    def test_mobile_workspace_contract_keeps_primary_operations_reachable(self):
        script = (API / "static" / "app.js").read_text(encoding="utf-8")
        styles = (API / "static" / "styles.css").read_text(encoding="utf-8")
        for contract in (
            "#deal-workspace", "#cash-movement-form", "#obligation-create-form",
            "data-settle-obligation", "data-reverse-movement", "draft-diff",
            "data-segment=\"all\"", "data-segment=\"under_80k\"", "data-segment=\"over_80k\"",
            "/api/pipeline", "/api/pipeline/deals", "/cashflow", "/preview",
            "navigate('deals')", "new URLSearchParams(window.location.search)",
            "href=\"/?deal=${encodeURIComponent(deal.id)}\"", "id=\"delete-deal\"",
            "confirmation:'DELETE'", "delete v.price_floor_override_reason",
        ):
            self.assertIn(contract, script)
        self.assertIn("@media(max-width:600px)", styles)
        self.assertIn(".cash-history{display:block}", styles)
        self.assertIn(".cash-history .actions button{width:100%}", styles)
        self.assertIn(".draft-card .actions button{width:100%}", styles)
        checked = subprocess.run(["node", "--check", str(API / "static" / "app.js")],
                                 capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
