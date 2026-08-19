-- Immutable economics configuration and calculation history.

CREATE TABLE IF NOT EXISTS economics_settings_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version INTEGER NOT NULL UNIQUE,
    tax_percent NUMERIC(9,6) NOT NULL CHECK (tax_percent >= 0),
    reserve_percent NUMERIC(9,6) NOT NULL CHECK (reserve_percent >= 0),
    rent_percent NUMERIC(9,6) NOT NULL CHECK (rent_percent >= 0),
    manager_percent NUMERIC(9,6) NOT NULL CHECK (manager_percent >= 0),
    marketing_percent NUMERIC(9,6) NOT NULL CHECK (marketing_percent >= 0),
    measure_percent NUMERIC(9,6) NOT NULL CHECK (measure_percent >= 0),
    owner_ae_share_percent NUMERIC(9,6) NOT NULL CHECK (owner_ae_share_percent > 0 AND owner_ae_share_percent <= 100),
    partner_installation_share_percent NUMERIC(9,6) NOT NULL DEFAULT 0 CHECK (partner_installation_share_percent >= 0 AND partner_installation_share_percent <= 100),
    golden_ae_percent NUMERIC(9,6) NOT NULL DEFAULT 10 CHECK (golden_ae_percent >= 0),
    take_ae_percent NUMERIC(9,6) NOT NULL DEFAULT 8 CHECK (take_ae_percent >= 0),
    max_raise_price_delta_percent NUMERIC(9,6) NOT NULL DEFAULT 15 CHECK (max_raise_price_delta_percent >= 0),
    income_recognition_stage TEXT NOT NULL DEFAULT 'installed',
    goal_owner_income NUMERIC(14,2) NOT NULL DEFAULT 800000 CHECK (goal_owner_income >= 0),
    goal_floor NUMERIC(14,2) NOT NULL DEFAULT 400000 CHECK (goal_floor >= 0),
    goal_stretch NUMERIC(14,2) NOT NULL DEFAULT 1100000 CHECK (goal_stretch >= 0),
    goal_deadline DATE NOT NULL DEFAULT DATE '2026-10-31',
    created_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (income_recognition_stage IN ('new_lead', 'contacted', 'qualified', 'measure_scheduled',
        'measure_completed', 'proposal_sent', 'decision_pending', 'contract_signed',
        'prepayment_received', 'production', 'installation_scheduled', 'installed',
        'closed_won', 'closed_lost', 'disqualified'))
);

INSERT INTO economics_settings_versions(
    version, tax_percent, reserve_percent, rent_percent, manager_percent,
    marketing_percent, measure_percent, owner_ae_share_percent
) VALUES (1, 6, 3, 5, 3, 5, 3, 40)
ON CONFLICT(version) DO NOTHING;

CREATE TABLE IF NOT EXISTS deal_economics_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID NOT NULL REFERENCES deals(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    settings_version INTEGER NOT NULL REFERENCES economics_settings_versions(version) ON DELETE RESTRICT,
    installation_mode TEXT NOT NULL CHECK (installation_mode IN ('solo', 'with_partner', 'external_team', 'unknown')),
    quoted_price NUMERIC(14,2) NOT NULL CHECK (quoted_price >= 0),
    materials_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (materials_cost >= 0),
    production_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (production_cost >= 0),
    seamstress_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (seamstress_cost >= 0),
    installation_direct_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (installation_direct_cost >= 0),
    fuel_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (fuel_cost >= 0),
    other_direct_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (other_direct_cost >= 0),
    tax_percent NUMERIC(9,6) NOT NULL,
    reserve_percent NUMERIC(9,6) NOT NULL,
    rent_percent NUMERIC(9,6) NOT NULL,
    manager_percent NUMERIC(9,6) NOT NULL,
    marketing_percent NUMERIC(9,6) NOT NULL,
    measure_percent NUMERIC(9,6) NOT NULL,
    owner_ae_share_percent NUMERIC(9,6) NOT NULL,
    partner_installation_share_percent NUMERIC(9,6) NOT NULL,
    director_profit NUMERIC(14,2),
    ae_amount NUMERIC(14,2),
    ae_percent NUMERIC(12,8),
    price_floor_ae_8 NUMERIC(14,2),
    price_floor_ae_10 NUMERIC(14,2),
    price_floor_ae_12 NUMERIC(14,2),
    owner_income_solo NUMERIC(14,2),
    owner_income_with_partner NUMERIC(14,2),
    projected_owner_income NUMERIC(14,2),
    economics_status TEXT NOT NULL CHECK (economics_status IN ('unknown', 'golden', 'take', 'raise_price', 'rebuild_or_reject')),
    calculation_error TEXT,
    created_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(deal_id, revision)
);

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS current_economics_revision_id UUID;
DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_current_economics_revision_fk
        FOREIGN KEY (current_economics_revision_id) REFERENCES deal_economics_revisions(id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS deal_economics_revisions_deal_created_idx
    ON deal_economics_revisions(deal_id, created_at DESC);

CREATE TABLE IF NOT EXISTS deal_income_recognitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID NOT NULL UNIQUE REFERENCES deals(id) ON DELETE RESTRICT,
    economics_revision_id UUID NOT NULL REFERENCES deal_economics_revisions(id) ON DELETE RESTRICT,
    settings_version INTEGER NOT NULL REFERENCES economics_settings_versions(version) ON DELETE RESTRICT,
    recognition_stage TEXT NOT NULL,
    recognized_owner_income NUMERIC(14,2) NOT NULL,
    recognized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_deal_economics_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'deal_economics_revisions are immutable; create a new revision instead';
END $$;

DROP TRIGGER IF EXISTS deal_economics_revisions_immutable ON deal_economics_revisions;
CREATE TRIGGER deal_economics_revisions_immutable
    BEFORE UPDATE OR DELETE ON deal_economics_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_deal_economics_revision_mutation();

CREATE OR REPLACE FUNCTION reject_deal_income_recognition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'deal_income_recognitions are immutable; create a correcting recognition separately';
END $$;

DROP TRIGGER IF EXISTS deal_income_recognitions_immutable ON deal_income_recognitions;
CREATE TRIGGER deal_income_recognitions_immutable
    BEFORE UPDATE OR DELETE ON deal_income_recognitions
    FOR EACH ROW EXECUTE FUNCTION reject_deal_income_recognition_mutation();
