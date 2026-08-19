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
