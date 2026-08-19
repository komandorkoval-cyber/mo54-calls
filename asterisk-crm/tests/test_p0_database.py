"""PostgreSQL integration tests for replay-safe P0 migrations.

The test launches one disposable local postgres:16 container, does not mount
application data, and removes the container in ``tearDownClass``. Run with:
``python -m unittest tests/test_p0_database.py`` from ``asterisk-crm``.
"""
from __future__ import annotations

import subprocess
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "sql"
FIXTURE_006 = ROOT / "tests" / "fixtures" / "006_ai_action_drafts.sql"
BASE = [SQL / f"{number:03d}_{name}.sql" for number, name in [
    (1, "create_calls"), (2, "crm_domain"), (3, "seed_admin"), (4, "novofon_integration"),
]]
P0_BEFORE_010 = [
    SQL / "007_p0_funnel_qualification.sql",
    SQL / "008_p0_economics_revisions.sql",
    SQL / "009_p0_cashflow_ledger.sql",
]
P0_010 = SQL / "010_p0_ai_proposals_audit.sql"


class PostgresHarness:
    def __init__(self):
        self.container = f"mo54-p0-test-{uuid.uuid4().hex[:10]}"

    def start(self):
        subprocess.run([
            "docker", "run", "--rm", "-d", "--name", self.container,
            "-e", "POSTGRES_PASSWORD=test", "postgres:16-alpine",
        ], check=True, capture_output=True, text=True)
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", self.container, "pg_isready", "-U", "postgres"],
                                   capture_output=True, text=True)
            if ready.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError("Disposable postgres did not become ready")

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.container], check=False, capture_output=True)

    def sql(self, database: str, source: str) -> str:
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database],
            input=source, text=True, encoding="utf-8", capture_output=True,
        )
        if result.returncode:
            raise AssertionError(f"psql failed:\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def execute_file(self, database: str, path: Path):
        self.sql(database, path.read_text(encoding="utf-8"))

    def create_database(self, name: str):
        self.sql("postgres", f'CREATE DATABASE "{name}";')

    def query(self, database: str, query: str) -> list[str]:
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "psql", "-At", "-U", "postgres", "-d", database, "-c", query],
            text=True, encoding="utf-8", capture_output=True,
        )
        if result.returncode:
            raise AssertionError(f"query failed:\n{result.stdout}\n{result.stderr}")
        return [line for line in result.stdout.splitlines() if line]


class P0DatabaseMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = PostgresHarness()
        cls.pg.start()

    @classmethod
    def tearDownClass(cls):
        cls.pg.stop()

    def apply_core_and_p0(self, database: str):
        self.pg.create_database(database)
        for path in [*BASE, *P0_BEFORE_010]:
            self.pg.execute_file(database, path)

    def ai_schema_signature(self, database: str) -> list[str]:
        return self.pg.query(database, """
            SELECT 'column|' || a.attname || '|' || pg_catalog.format_type(a.atttypid, a.atttypmod)
              FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
             WHERE c.relname='ai_action_drafts' AND a.attnum > 0 AND NOT a.attisdropped
            UNION ALL
            SELECT 'constraint|' || conname || '|' || pg_get_constraintdef(oid)
              FROM pg_constraint WHERE conrelid='ai_action_drafts'::regclass
            UNION ALL
            SELECT 'index|' || indexname || '|' || indexdef
              FROM pg_indexes WHERE tablename='ai_action_drafts'
            ORDER BY 1;
        """)

    def test_006_then_010_then_replay_matches_010_then_006_then_replay(self):
        first, second = "p0_006_010", "p0_010_006"
        self.apply_core_and_p0(first)
        for path in [FIXTURE_006, P0_010, FIXTURE_006, P0_010]:
            self.pg.execute_file(first, path)

        self.apply_core_and_p0(second)
        for path in [P0_010, FIXTURE_006, P0_010, FIXTURE_006]:
            self.pg.execute_file(second, path)

        self.assertEqual(self.ai_schema_signature(first), self.ai_schema_signature(second))
        kind_constraint = "\n".join(self.ai_schema_signature(first))
        self.assertIn("deal_update", kind_constraint)
        self.assertIn("UNIQUE (insight_id, kind)", kind_constraint)
        self.assertIn("ai_action_drafts_call_status_idx", kind_constraint)

    def test_full_p0_migration_set_replays_on_a_clean_database(self):
        database = "p0_full_replay"
        self.apply_core_and_p0(database)
        self.pg.execute_file(database, P0_010)
        for path in [*BASE, *P0_BEFORE_010, P0_010]:
            self.pg.execute_file(database, path)
        tables = self.pg.query(database, """
            SELECT tablename FROM pg_tables WHERE schemaname='public'
              AND tablename IN ('economics_settings_versions','deal_economics_revisions',
                'deal_income_recognitions','deal_cash_movements','deal_cost_obligations','ai_action_drafts')
            ORDER BY tablename;
        """)
        self.assertEqual(tables, [
            "ai_action_drafts", "deal_cash_movements", "deal_cost_obligations",
            "deal_economics_revisions", "deal_income_recognitions", "economics_settings_versions",
        ])

    def test_revisions_and_cash_movements_are_immutable(self):
        database = "p0_immutability"
        self.apply_core_and_p0(database)
        self.pg.execute_file(database, P0_010)
        self.pg.sql(database, """
            INSERT INTO crm_users(email,display_name,password_hash) VALUES('test@example.com','Test','x');
            INSERT INTO contacts(phone_normalized) VALUES('+79990000000');
            INSERT INTO deals(contact_id,title) SELECT id,'Terrace' FROM contacts LIMIT 1;
            INSERT INTO deal_economics_revisions(
              deal_id,revision,settings_version,installation_mode,quoted_price,
              tax_percent,reserve_percent,rent_percent,manager_percent,marketing_percent,measure_percent,
              owner_ae_share_percent,partner_installation_share_percent,economics_status
            ) SELECT id,1,1,'solo',100000,6,3,5,3,5,3,40,0,'golden' FROM deals LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'customer_incoming',100000,now() FROM deals LIMIT 1;
        """)
        revision_error = subprocess.run(
            ["docker", "exec", "-i", self.pg.container, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database],
            input="UPDATE deal_economics_revisions SET quoted_price=1;", text=True, encoding="utf-8", capture_output=True,
        )
        movement_error = subprocess.run(
            ["docker", "exec", "-i", self.pg.container, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database],
            input="DELETE FROM deal_cash_movements;", text=True, encoding="utf-8", capture_output=True,
        )
        self.assertNotEqual(revision_error.returncode, 0)
        self.assertIn("immutable", revision_error.stderr)
        self.assertNotEqual(movement_error.returncode, 0)
        self.assertIn("append-only", movement_error.stderr)

    def test_safe_cash_components_keep_realized_and_open_costs_separate(self):
        database = "p0_cashflow"
        self.apply_core_and_p0(database)
        self.pg.sql(database, """
            INSERT INTO contacts(phone_normalized) VALUES('+79990000001');
            INSERT INTO deals(contact_id,title) SELECT id,'Cashflow terrace' FROM contacts LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'customer_incoming',200,now() FROM deals LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'customer_refund',25,now() FROM deals LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'realized_cost_outflow',80,now() FROM deals LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'other_reserved_cash',20,now() FROM deals LIMIT 1;
            INSERT INTO deal_cash_movements(deal_id,kind,amount,confirmed_at)
              SELECT id,'other_reserved_cash_release',5,now() FROM deals LIMIT 1;
            INSERT INTO deal_cost_obligations(deal_id,amount,status)
              SELECT id,40,'open' FROM deals LIMIT 1;
            INSERT INTO deal_cost_obligations(deal_id,amount,status,settled_at)
              SELECT id,30,'settled',now() FROM deals LIMIT 1;
        """)
        result = self.pg.query(database, """
            WITH movement_totals AS (
              SELECT deal_id,
                coalesce(sum(amount) FILTER (WHERE kind='customer_incoming'),0)
                  - coalesce(sum(amount) FILTER (WHERE kind='customer_refund'),0) AS net_customer_cash,
                coalesce(sum(amount) FILTER (WHERE kind='realized_cost_outflow'),0) AS realized_cost_outflows,
                coalesce(sum(amount) FILTER (WHERE kind='other_reserved_cash'),0)
                  - coalesce(sum(amount) FILTER (WHERE kind='other_reserved_cash_release'),0) AS other_reserved_cash
              FROM deal_cash_movements GROUP BY deal_id
            ), obligation_totals AS (
              SELECT deal_id,coalesce(sum(amount) FILTER (WHERE status='open'),0) AS open_reserved_obligations
              FROM deal_cost_obligations GROUP BY deal_id
            )
            SELECT net_customer_cash || '|' || realized_cost_outflows || '|' || open_reserved_obligations || '|'
              || other_reserved_cash || '|' || (net_customer_cash-realized_cost_outflows-open_reserved_obligations-other_reserved_cash)
            FROM movement_totals JOIN obligation_totals USING(deal_id);
        """)
        self.assertEqual(result, ["175.00|80.00|40.00|15.00|40.00"])

    def test_income_recognition_is_single_and_defaults_to_installed(self):
        database = "p0_recognition"
        self.apply_core_and_p0(database)
        self.pg.sql(database, """
            INSERT INTO contacts(phone_normalized) VALUES('+79990000002');
            INSERT INTO deals(contact_id,title) SELECT id,'Recognition terrace' FROM contacts LIMIT 1;
            INSERT INTO deal_economics_revisions(
              deal_id,revision,settings_version,installation_mode,quoted_price,
              tax_percent,reserve_percent,rent_percent,manager_percent,marketing_percent,measure_percent,
              owner_ae_share_percent,partner_installation_share_percent,projected_owner_income,economics_status
            ) SELECT id,1,1,'solo',100000,6,3,5,3,5,3,40,0,25000,'golden' FROM deals LIMIT 1;
            UPDATE deals SET current_economics_revision_id=(SELECT id FROM deal_economics_revisions LIMIT 1);
            INSERT INTO deal_income_recognitions(
              deal_id,economics_revision_id,settings_version,recognition_stage,recognized_owner_income
            ) SELECT d.id,d.current_economics_revision_id,1,'installed',25000 FROM deals d;
            INSERT INTO deal_income_recognitions(
              deal_id,economics_revision_id,settings_version,recognition_stage,recognized_owner_income
            ) SELECT d.id,d.current_economics_revision_id,1,'installed',99999 FROM deals d
              ON CONFLICT(deal_id) DO NOTHING;
        """)
        self.assertEqual(self.pg.query(database, "SELECT income_recognition_stage FROM economics_settings_versions WHERE version=1;"), ["installed"])
        self.assertEqual(self.pg.query(database, "SELECT recognized_owner_income::text FROM deal_income_recognitions;"), ["25000.00"])


if __name__ == "__main__":
    unittest.main()
