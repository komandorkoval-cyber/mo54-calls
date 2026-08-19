-- P0 commercial funnel and qualification.  This migration is additive: legacy
-- deal stages and their historical values intentionally remain untouched.

ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'new_lead';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'contacted';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'measure_scheduled';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'measure_completed';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'proposal_sent';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'decision_pending';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'contract_signed';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'prepayment_received';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'production';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'installation_scheduled';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'installed';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'closed_won';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'closed_lost';
ALTER TYPE deal_stage ADD VALUE IF NOT EXISTS 'disqualified';

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS qualification_segment TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS object_type TEXT,
    ADD COLUMN IF NOT EXISTS property_type TEXT,
    ADD COLUMN IF NOT EXISTS estimated_budget_min NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS estimated_budget_max NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS budget_range TEXT,
    ADD COLUMN IF NOT EXISTS estimated_area_m2 NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS location_text TEXT,
    ADD COLUMN IF NOT EXISTS desired_install_date DATE,
    ADD COLUMN IF NOT EXISTS desired_install_period TEXT,
    ADD COLUMN IF NOT EXISTS qualification_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS qualification_reason TEXT,
    ADD COLUMN IF NOT EXISTS decision_makers JSONB,
    ADD COLUMN IF NOT EXISTS decision_maker_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS photos_received BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS measurements_received BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pain_primary TEXT,
    ADD COLUMN IF NOT EXISTS pain_secondary JSONB,
    ADD COLUMN IF NOT EXISTS customer_quote TEXT,
    ADD COLUMN IF NOT EXISTS customer_quote_evidence JSONB,
    ADD COLUMN IF NOT EXISTS alternative_considered TEXT,
    ADD COLUMN IF NOT EXISTS alternative_reason TEXT,
    ADD COLUMN IF NOT EXISTS urgency_reason TEXT,
    ADD COLUMN IF NOT EXISTS offer_package TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS quoted_price NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS final_contract_price NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS lead_source TEXT,
    ADD COLUMN IF NOT EXISTS utm_source TEXT,
    ADD COLUMN IF NOT EXISTS utm_medium TEXT,
    ADD COLUMN IF NOT EXISTS utm_campaign TEXT,
    ADD COLUMN IF NOT EXISTS utm_content TEXT,
    ADD COLUMN IF NOT EXISTS utm_term TEXT,
    ADD COLUMN IF NOT EXISTS yandex_campaign_id TEXT,
    ADD COLUMN IF NOT EXISTS yandex_adgroup_id TEXT,
    ADD COLUMN IF NOT EXISTS yandex_keyword TEXT,
    ADD COLUMN IF NOT EXISTS search_query TEXT,
    ADD COLUMN IF NOT EXISTS installation_mode TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS installation_slots_required NUMERIC(8,2),
    ADD COLUMN IF NOT EXISTS installation_hours_estimate NUMERIC(8,2),
    ADD COLUMN IF NOT EXISTS installation_planned_date DATE,
    ADD COLUMN IF NOT EXISTS installation_completed_date DATE,
    ADD COLUMN IF NOT EXISTS next_contact_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS disqualification_reason TEXT;

DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_qualification_segment_check
        CHECK (qualification_segment IN ('under_80k', 'over_80k', 'unknown'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_qualification_status_check
        CHECK (qualification_status IN ('unknown', 'unqualified', 'qualified', 'high_priority'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_decision_maker_status_check
        CHECK (decision_maker_status IN ('unknown', 'single', 'multiple', 'other_person_required'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_offer_package_check
        CHECK (offer_package IN ('custom', 'good', 'better', 'best', 'unknown'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE deals ADD CONSTRAINT deals_installation_mode_check
        CHECK (installation_mode IN ('solo', 'with_partner', 'external_team', 'unknown'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS deal_reason_catalog (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN ('lost', 'disqualified')),
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(kind, code)
);

INSERT INTO deal_reason_catalog(kind, code, label) VALUES
    ('lost', 'price_too_high', 'Цена слишком высокая'),
    ('lost', 'competitor_chosen', 'Выбран конкурент'),
    ('lost', 'glass_solution_chosen', 'Выбрано стеклянное решение'),
    ('lost', 'postponed', 'Отложено'),
    ('lost', 'no_decision', 'Нет решения'),
    ('lost', 'spouse_or_partner_not_agreed', 'Не согласовано с супругом/партнёром'),
    ('lost', 'timing_not_suitable', 'Не подходят сроки'),
    ('lost', 'no_contact', 'Нет контакта'),
    ('lost', 'trust_issue', 'Недостаток доверия'),
    ('lost', 'product_not_suitable', 'Продукт не подходит'),
    ('lost', 'other', 'Другое'),
    ('disqualified', 'budget_below_80k', 'Бюджет ниже 80k'),
    ('disqualified', 'small_object', 'Малый объект'),
    ('disqualified', 'outside_geography', 'Вне географии'),
    ('disqualified', 'not_terrace_related', 'Не связано с террасой'),
    ('disqualified', 'no_real_need', 'Нет реальной потребности'),
    ('disqualified', 'unrealistic_timing', 'Нереалистичный срок'),
    ('disqualified', 'bad_fit', 'Не подходит профилю'),
    ('disqualified', 'other', 'Другое')
ON CONFLICT(kind, code) DO NOTHING;

CREATE INDEX IF NOT EXISTS deals_qualification_segment_idx
    ON deals(qualification_segment, updated_at DESC);
CREATE INDEX IF NOT EXISTS deals_stage_owner_idx
    ON deals(stage, owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS deals_installation_planned_idx
    ON deals(installation_planned_date) WHERE installation_planned_date IS NOT NULL;
