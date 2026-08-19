-- A below-floor price may be recorded only with an explicit, auditable reason.
ALTER TABLE deals ADD COLUMN IF NOT EXISTS price_floor_override_reason TEXT;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS price_floor_overridden_at TIMESTAMPTZ;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS price_floor_overridden_by UUID REFERENCES crm_users(id) ON DELETE SET NULL;
