-- Deal-update proposals preserve a field-level baseline so approval can reject
-- stale writes instead of silently overwriting a manager's later changes.
ALTER TABLE ai_action_drafts
    ADD COLUMN IF NOT EXISTS base_deal_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS proposal_schema_version TEXT;

CREATE INDEX IF NOT EXISTS deal_reason_catalog_active_kind_idx
    ON deal_reason_catalog(kind, code)
    WHERE active;
