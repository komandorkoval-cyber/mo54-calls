-- This bootstrap is semantically identical to 006_ai_action_drafts.sql.
-- It makes P0 independently runnable from the clean base while remaining
-- order-compatible with 006 when the local-agent work is integrated later.
CREATE TABLE IF NOT EXISTS ai_action_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    insight_id UUID NOT NULL REFERENCES call_insights(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('task_create', 'deal_create', 'contact_update')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    payload JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    applied_entity_type TEXT,
    applied_entity_id TEXT,
    reviewed_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(insight_id, kind)
);

CREATE INDEX IF NOT EXISTS ai_action_drafts_call_status_idx
    ON ai_action_drafts(call_id, status, created_at DESC);

ALTER TABLE ai_action_drafts
    ADD COLUMN IF NOT EXISTS target_deal_id UUID REFERENCES deals(id) ON DELETE CASCADE;
ALTER TABLE ai_action_drafts DROP CONSTRAINT IF EXISTS ai_action_drafts_kind_check;
ALTER TABLE ai_action_drafts ADD CONSTRAINT ai_action_drafts_kind_check
    CHECK (kind IN ('task_create', 'deal_create', 'contact_update', 'deal_update'));

CREATE INDEX IF NOT EXISTS ai_action_drafts_target_deal_status_idx
    ON ai_action_drafts(target_deal_id, status, created_at DESC)
    WHERE target_deal_id IS NOT NULL;
