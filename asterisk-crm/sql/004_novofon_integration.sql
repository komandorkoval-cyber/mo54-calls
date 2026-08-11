-- Durable provider-integration state. This file is intentionally idempotent:
-- the compose migration runner replays every SQL file on each deployment.

ALTER TABLE crm_users
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT TRUE;

-- Browser credentials are opaque, server-side sessions.  Only their SHA-256
-- digest is stored, so a database read alone cannot impersonate a manager.
-- This also makes logout and a password change reliably revoke prior sessions.
CREATE TABLE IF NOT EXISTS crm_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES crm_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS crm_sessions_active_user_idx
    ON crm_sessions(user_id, expires_at DESC)
    WHERE revoked_at IS NULL;

-- Novofon records arrive in several independent notifications.  These states
-- deliberately sit alongside the local-ASR states from 002 rather than
-- pretending that a provider recording has already been transcribed.
ALTER TYPE call_processing_status ADD VALUE IF NOT EXISTS 'awaiting_recording';
ALTER TYPE call_processing_status ADD VALUE IF NOT EXISTS 'awaiting_transcript';

CREATE TABLE IF NOT EXISTS provider_employee_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL DEFAULT 'novofon',
    crm_user_id UUID NOT NULL REFERENCES crm_users(id) ON DELETE CASCADE,
    provider_employee_id TEXT NOT NULL,
    provider_user_id TEXT,
    provider_extension TEXT,
    display_name TEXT,
    provider_status TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    provider_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, crm_user_id, provider_employee_id)
);

-- A current provider employee maps to at most one CRM user, and vice versa.
-- Inactive rows retain mapping history for call/event attribution.
CREATE UNIQUE INDEX IF NOT EXISTS provider_employee_mappings_active_employee_uidx
    ON provider_employee_mappings(provider, provider_employee_id)
    WHERE active;
CREATE UNIQUE INDEX IF NOT EXISTS provider_employee_mappings_active_user_uidx
    ON provider_employee_mappings(provider, crm_user_id)
    WHERE active;
CREATE INDEX IF NOT EXISTS provider_employee_mappings_extension_idx
    ON provider_employee_mappings(provider, provider_extension)
    WHERE active AND provider_extension IS NOT NULL;

CREATE TABLE IF NOT EXISTS provider_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL DEFAULT 'novofon',
    provider TEXT NOT NULL DEFAULT 'novofon',
    idempotency_key TEXT NOT NULL,
    external_event_id TEXT,
    event_type TEXT NOT NULL,
    call_session_id TEXT,
    recording_url TEXT,
    recording_link_hash TEXT,
    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    signature_valid BOOLEAN,
    provider_status TEXT,
    provider_code TEXT,
    call_id BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    employee_mapping_id UUID REFERENCES provider_employee_mappings(id) ON DELETE SET NULL,
    processing_status TEXT NOT NULL DEFAULT 'received',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    processing_error TEXT,
    processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, idempotency_key)
);

-- Delivery IDs are preferred when supplied. The natural-event key protects
-- duplicate webhook deliveries even where the provider does not supply one.
CREATE UNIQUE INDEX IF NOT EXISTS provider_events_source_external_event_uidx
    ON provider_events(source, external_event_id)
    WHERE external_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS provider_events_natural_event_uidx
    ON provider_events(source, event_type, call_session_id, COALESCE(recording_link_hash, ''))
    WHERE call_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_events_processing_queue_idx
    ON provider_events(source, processing_status, next_attempt_at, received_at)
    WHERE processing_status IN ('received', 'retry');
CREATE INDEX IF NOT EXISTS provider_events_call_idx
    ON provider_events(call_id, received_at DESC)
    WHERE call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_events_call_session_idx
    ON provider_events(source, call_session_id, received_at DESC)
    WHERE call_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS provider_recordings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL DEFAULT 'novofon',
    provider TEXT NOT NULL DEFAULT 'novofon',
    provider_recording_id TEXT NOT NULL,
    provider_call_id TEXT,
    call_session_id TEXT,
    recording_url TEXT,
    recording_link_hash TEXT,
    call_id BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    source_event_id UUID REFERENCES provider_events(id) ON DELETE SET NULL,
    recording_id UUID REFERENCES recordings(id) ON DELETE SET NULL,
    channel TEXT NOT NULL DEFAULT 'mixed',
    provider_status TEXT,
    mime_type TEXT,
    duration_sec INTEGER CHECK (duration_sec IS NULL OR duration_sec >= 0),
    size_bytes BIGINT CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT,
    available_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    download_status TEXT NOT NULL DEFAULT 'pending',
    downloaded_at TIMESTAMPTZ,
    last_error TEXT,
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_recording_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_recordings_source_link_uidx
    ON provider_recordings(source, recording_link_hash)
    WHERE recording_link_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_recordings_provider_call_idx
    ON provider_recordings(provider, provider_call_id)
    WHERE provider_call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_recordings_call_session_idx
    ON provider_recordings(source, call_session_id, created_at DESC)
    WHERE call_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_recordings_call_idx
    ON provider_recordings(call_id, created_at DESC)
    WHERE call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_recordings_download_queue_idx
    ON provider_recordings(source, download_status, available_at, created_at)
    WHERE download_status IN ('pending', 'retry');

CREATE TABLE IF NOT EXISTS call_initiation_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL DEFAULT 'novofon',
    provider TEXT NOT NULL DEFAULT 'novofon',
    idempotency_key TEXT NOT NULL,
    provider_request_id TEXT,
    call_session_id TEXT,
    requested_by_user_id UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    employee_mapping_id UUID REFERENCES provider_employee_mappings(id) ON DELETE SET NULL,
    contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
    deal_id UUID REFERENCES deals(id) ON DELETE SET NULL,
    call_id BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    from_number TEXT,
    to_number TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    provider_status TEXT,
    intent JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_request JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_response JSONB,
    last_error TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS call_initiation_requests_source_provider_request_uidx
    ON call_initiation_requests(source, provider_request_id)
    WHERE provider_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS call_initiation_requests_queue_idx
    ON call_initiation_requests(source, status, requested_at)
    WHERE status IN ('requested', 'retry');
CREATE INDEX IF NOT EXISTS call_initiation_requests_call_idx
    ON call_initiation_requests(call_id, requested_at DESC)
    WHERE call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS call_initiation_requests_call_session_idx
    ON call_initiation_requests(source, call_session_id, requested_at DESC)
    WHERE call_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS call_initiation_requests_contact_idx
    ON call_initiation_requests(contact_id, requested_at DESC)
    WHERE contact_id IS NOT NULL;
