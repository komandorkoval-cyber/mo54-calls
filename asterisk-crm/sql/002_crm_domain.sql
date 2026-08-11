CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE crm_role AS ENUM ('admin', 'manager');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE call_processing_status AS ENUM
        ('received', 'recorded', 'transcribing', 'analyzing', 'ready', 'failed', 'ignored');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('pending', 'running', 'retry', 'completed', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE job_kind AS ENUM ('transcribe', 'analyze');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE deal_stage AS ENUM
        ('new', 'qualified', 'proposal', 'negotiation', 'won', 'lost');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE task_status AS ENUM ('open', 'completed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS crm_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role crm_role NOT NULL DEFAULT 'manager',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    full_name TEXT,
    phone_normalized TEXT UNIQUE NOT NULL,
    phone_raw TEXT,
    email TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE calls ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'asterisk';
ALTER TABLE calls ADD COLUMN IF NOT EXISTS external_call_id TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES crm_users(id) ON DELETE SET NULL;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS processing_status call_processing_status NOT NULL DEFAULT 'received';
ALTER TABLE calls ADD COLUMN IF NOT EXISTS processing_error TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS answered BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
UPDATE calls SET external_call_id = call_id WHERE external_call_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS calls_source_external_uidx
    ON calls(source, external_call_id);
CREATE INDEX IF NOT EXISTS calls_contact_idx ON calls(contact_id);
CREATE INDEX IF NOT EXISTS calls_processing_idx ON calls(processing_status, started_at DESC);

CREATE TABLE IF NOT EXISTS recordings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('customer', 'manager', 'mixed')),
    storage_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'audio/wav',
    duration_sec INTEGER,
    size_bytes BIGINT,
    sha256 TEXT,
    retention_until DATE DEFAULT (current_date + 180),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(call_id, channel)
);

CREATE TABLE IF NOT EXISTS transcripts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    asr_model TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'ru',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(call_id, version)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id BIGSERIAL PRIMARY KEY,
    transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL CHECK (speaker IN ('customer', 'manager', 'unknown')),
    started_ms INTEGER,
    ended_ms INTEGER,
    text TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    UNIQUE(transcript_id, ordinal)
);

CREATE TABLE IF NOT EXISTS call_insights (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    data JSONB NOT NULL,
    confidence NUMERIC(4,3),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(call_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS call_insights_one_current_idx
    ON call_insights(call_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS deals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    owner_id UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    stage deal_stage NOT NULL DEFAULT 'new',
    amount NUMERIC(14,2),
    probability SMALLINT CHECK (probability BETWEEN 0 AND 100),
    loss_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS call_deals (
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    deal_id UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    PRIMARY KEY(call_id, deal_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    deal_id UUID REFERENCES deals(id) ON DELETE CASCADE,
    call_id BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    assignee_id UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT,
    due_at TIMESTAMPTZ,
    status task_status NOT NULL DEFAULT 'open',
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_work_queue_idx ON tasks(status, due_at);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    kind job_kind NOT NULL,
    status job_status NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    last_error TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(call_id, kind)
);
CREATE INDEX IF NOT EXISTS processing_jobs_claim_idx
    ON processing_jobs(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_id UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_data JSONB,
    after_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION normalize_phone(raw TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN regexp_replace(coalesce(raw, ''), '\D', '', 'g') ~ '^[78]\d{10}$'
            THEN '+7' || right(regexp_replace(raw, '\D', '', 'g'), 10)
        WHEN regexp_replace(coalesce(raw, ''), '\D', '', 'g') <> ''
            THEN '+' || regexp_replace(raw, '\D', '', 'g')
        ELSE 'unknown'
    END
$$;

CREATE OR REPLACE FUNCTION ingest_asterisk_call(
    p_external_call_id TEXT,
    p_direction TEXT,
    p_caller TEXT,
    p_callee TEXT,
    p_started_at TIMESTAMPTZ,
    p_duration_sec INTEGER,
    p_recording_rx TEXT,
    p_recording_tx TEXT
) RETURNS BIGINT LANGUAGE plpgsql AS $$
DECLARE
    v_phone TEXT;
    v_contact UUID;
    v_call BIGINT;
BEGIN
    IF coalesce(p_duration_sec, 0) <= 0 OR p_direction NOT IN ('in', 'out') THEN
        RETURN NULL;
    END IF;
    v_phone := normalize_phone(CASE WHEN p_direction = 'in' THEN p_caller ELSE p_callee END);
    INSERT INTO contacts(phone_normalized, phone_raw)
    VALUES(v_phone, CASE WHEN p_direction = 'in' THEN p_caller ELSE p_callee END)
    ON CONFLICT(phone_normalized) DO UPDATE SET updated_at = now()
    RETURNING id INTO v_contact;

    INSERT INTO calls(
        call_id, source, external_call_id, direction, caller_number, callee_number,
        contact_id, started_at, ended_at, duration_sec, recording_in, recording_out,
        processing_status, status
    ) VALUES(
        p_external_call_id, 'asterisk', p_external_call_id, p_direction,
        p_caller, p_callee, v_contact, p_started_at,
        p_started_at + make_interval(secs => p_duration_sec), p_duration_sec,
        p_recording_rx, p_recording_tx, 'recorded', 'new'
    )
    ON CONFLICT(source, external_call_id) DO UPDATE SET updated_at = now()
    RETURNING id INTO v_call;

    INSERT INTO recordings(call_id, channel, storage_path)
    VALUES(v_call, 'customer', p_recording_rx), (v_call, 'manager', p_recording_tx)
    ON CONFLICT(call_id, channel) DO NOTHING;
    INSERT INTO processing_jobs(call_id, kind) VALUES(v_call, 'transcribe')
    ON CONFLICT(call_id, kind) DO NOTHING;
    RETURN v_call;
END $$;

-- Migrate legacy rows without overwriting user-entered data.
INSERT INTO contacts(phone_normalized, phone_raw)
SELECT DISTINCT normalize_phone(CASE WHEN direction = 'in' THEN caller_number ELSE callee_number END),
       CASE WHEN direction = 'in' THEN caller_number ELSE callee_number END
FROM calls
WHERE normalize_phone(CASE WHEN direction = 'in' THEN caller_number ELSE callee_number END) <> 'unknown'
ON CONFLICT(phone_normalized) DO NOTHING;

UPDATE calls c SET contact_id = ct.id
FROM contacts ct
WHERE c.contact_id IS NULL
  AND ct.phone_normalized = normalize_phone(
      CASE WHEN c.direction = 'in' THEN c.caller_number ELSE c.callee_number END);

INSERT INTO recordings(call_id, channel, storage_path)
SELECT id, 'customer', recording_in FROM calls WHERE recording_in IS NOT NULL
ON CONFLICT(call_id, channel) DO NOTHING;
INSERT INTO recordings(call_id, channel, storage_path)
SELECT id, 'manager', recording_out FROM calls WHERE recording_out IS NOT NULL
ON CONFLICT(call_id, channel) DO NOTHING;
