-- Manual CRM contacts may have a customer, assistant, and other reachable
-- numbers.  The legacy contacts.phone_normalized column remains the primary
-- display projection for existing joins; child rows are the canonical source
-- for matching calls and initiating a selected callback.
--
-- Do not auto-merge old contacts.  Before this migration changes anything,
-- stop if two legacy rows are merely different textual forms of one phone.
-- A manager must resolve such an ambiguous customer identity explicitly.
BEGIN;

-- The deployment runner invokes each migration file in autocommit mode.  Keep
-- this entire compatibility migration atomic and block only Calls writers
-- while its legacy collision check/backfill runs.  Reads continue normally;
-- an old API/worker cannot insert a legacy contact between the guard and its
-- canonical projection update.
LOCK TABLE contacts, calls, call_initiation_requests IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    v_collision TEXT;
BEGIN
    -- Keep this pre-schema check semantic with canonical_contact_phone below.
    -- Novofon strips an international 00 prefix before reporting an event, so
    -- a manually entered 00-prefixed number must resolve to the same client.
    WITH raw AS (
        SELECT id, regexp_replace(coalesce(phone_normalized, ''), '\D', '', 'g') AS digits
          FROM contacts
    ), normalized AS (
        SELECT id, CASE WHEN digits LIKE '00%' THEN substring(digits FROM 3) ELSE digits END AS digits
          FROM raw
    ), legacy AS (
        SELECT id,
               CASE
                   WHEN digits ~ '^\d{10}$' THEN '7' || digits
                   WHEN digits ~ '^[78]\d{10}$' THEN '7' || right(digits, 10)
                   WHEN digits ~ '^\d{11,15}$' THEN digits
                   ELSE NULL
               END AS phone_key
          FROM normalized
    ), collisions AS (
        SELECT phone_key, array_agg(id::text ORDER BY id) AS contact_ids
          FROM legacy
         WHERE phone_key IS NOT NULL
         GROUP BY phone_key
        HAVING count(*) > 1
         LIMIT 1
    )
    SELECT phone_key || ': ' || array_to_string(contact_ids, ', ') INTO v_collision
      FROM collisions;

    IF v_collision IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'integrity_constraint_violation',
            MESSAGE = 'Cannot migrate manual contact phone numbers: legacy canonical-phone collision',
            DETAIL = v_collision,
            HINT = 'Resolve the listed duplicate contacts manually; this migration never auto-merges customer histories.';
    END IF;
END $$;

-- CRM stores contact phones in a single canonical key.  Provider calls still
-- receive digits without "+" at the API boundary; that conversion is kept out
-- of the database identity model deliberately.
CREATE OR REPLACE FUNCTION canonical_contact_phone(raw TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    WITH raw_digits AS (
        SELECT regexp_replace(coalesce(raw, ''), '\D', '', 'g') AS value
    ), digits AS (
        SELECT CASE WHEN value LIKE '00%' THEN substring(value FROM 3) ELSE value END AS value
          FROM raw_digits
    )
    SELECT CASE
        WHEN value ~ '^\d{10}$' THEN '7' || value
        WHEN value ~ '^[78]\d{10}$' THEN '7' || right(value, 10)
        WHEN value ~ '^\d{11,15}$' THEN value
        ELSE NULL
    END
      FROM digits
$$;

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES crm_users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS contacts_owner_idx ON contacts(owner_id) WHERE owner_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS contact_phone_numbers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    phone_key TEXT NOT NULL UNIQUE,
    phone_normalized TEXT NOT NULL,
    phone_raw TEXT,
    label TEXT NOT NULL DEFAULT 'Основной',
    role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer', 'assistant', 'other')),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT contact_phone_numbers_phone_key_matches_normalized
        CHECK (
            canonical_contact_phone(phone_normalized) IS NOT NULL
            AND phone_key = canonical_contact_phone(phone_normalized)
        ),
    CONSTRAINT contact_phone_numbers_id_contact_id_key UNIQUE (id, contact_id)
);
CREATE INDEX IF NOT EXISTS contact_phone_numbers_contact_idx
    ON contact_phone_numbers(contact_id, active DESC, is_primary DESC, created_at ASC);
CREATE UNIQUE INDEX IF NOT EXISTS contact_phone_numbers_one_active_primary_idx
    ON contact_phone_numbers(contact_id)
    WHERE active AND is_primary;

-- Backfill once.  Replay leaves any subsequent user edits untouched.
INSERT INTO contact_phone_numbers(
    contact_id, phone_key, phone_normalized, phone_raw, label, role,
    is_primary, active, created_by_user_id
)
SELECT id,
       canonical_contact_phone(phone_normalized),
       '+' || canonical_contact_phone(phone_normalized),
       phone_raw,
       'Основной',
       'customer',
       TRUE,
       TRUE,
       owner_id
  FROM contacts
 WHERE canonical_contact_phone(phone_normalized) IS NOT NULL
ON CONFLICT (phone_key) DO NOTHING;

-- Bring the legacy primary projection into the same E.164 display form.  The
-- collision guard above proves this cannot turn two rows into one unique key.
UPDATE contacts
   SET phone_normalized = '+' || canonical_contact_phone(phone_normalized),
       updated_at = now()
 WHERE canonical_contact_phone(phone_normalized) IS NOT NULL
   AND phone_normalized IS DISTINCT FROM '+' || canonical_contact_phone(phone_normalized);

ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS contact_phone_number_id UUID
    REFERENCES contact_phone_numbers(id) ON DELETE SET NULL;
ALTER TABLE call_initiation_requests
    ADD COLUMN IF NOT EXISTS contact_phone_number_id UUID
    REFERENCES contact_phone_numbers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS calls_contact_phone_number_idx
    ON calls(contact_phone_number_id) WHERE contact_phone_number_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS call_initiation_requests_contact_phone_number_idx
    ON call_initiation_requests(contact_phone_number_id, requested_at DESC)
    WHERE contact_phone_number_id IS NOT NULL;

-- A phone identity always belongs to the same parent client as the call or
-- initiation it annotates.  Individual FKs remain useful for rows without a
-- contact; this pair FK prevents a contact reassignment from leaving a stale
-- assistant/customer number attached to another client.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='calls_contact_phone_number_contact_fk'
    ) THEN
        ALTER TABLE calls
            ADD CONSTRAINT calls_contact_phone_number_contact_fk
            FOREIGN KEY (contact_phone_number_id, contact_id)
            REFERENCES contact_phone_numbers(id, contact_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='call_initiations_contact_phone_number_contact_fk'
    ) THEN
        ALTER TABLE call_initiation_requests
            ADD CONSTRAINT call_initiations_contact_phone_number_contact_fk
            FOREIGN KEY (contact_phone_number_id, contact_id)
            REFERENCES contact_phone_numbers(id, contact_id);
    END IF;
END $$;

-- Preserve the known number on historic rows where it can be established
-- deterministically from the already linked contact.
UPDATE calls c
   SET contact_phone_number_id = pn.id
  FROM contact_phone_numbers pn
 WHERE c.contact_phone_number_id IS NULL
   AND pn.contact_id = c.contact_id
   AND pn.phone_key = canonical_contact_phone(
       CASE WHEN c.direction::text = 'in' THEN c.caller_number ELSE c.callee_number END
   );

UPDATE call_initiation_requests cir
   SET contact_phone_number_id = pn.id
  FROM contact_phone_numbers pn
 WHERE cir.contact_phone_number_id IS NULL
   AND pn.contact_id = cir.contact_id
   AND pn.phone_key = canonical_contact_phone(cir.to_number);

-- Resolve an incoming/outgoing provider number to exactly one CRM contact.
-- Unknown valid numbers create one contact plus its immutable primary number;
-- a registered assistant/other number instead resolves to its parent client.
CREATE OR REPLACE FUNCTION contact_for_phone(
    p_phone TEXT,
    p_raw TEXT DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_key TEXT;
    v_display TEXT;
    v_contact UUID;
BEGIN
    v_key := canonical_contact_phone(p_phone);
    IF v_key IS NULL THEN
        RETURN NULL;
    END IF;
    v_display := '+' || v_key;

    -- Serialize all provider-side creations for one canonical phone.  This
    -- avoids concurrent Asterisk/Novofon deliveries producing two contacts
    -- before either sees the other's unique child-phone row.
    PERFORM pg_advisory_xact_lock(hashtext(v_key));

    SELECT contact_id INTO v_contact
      FROM contact_phone_numbers
     WHERE phone_key = v_key
     LIMIT 1;
    IF FOUND THEN
        UPDATE contact_phone_numbers
           SET phone_raw = coalesce(phone_raw, p_raw), updated_at = now()
         WHERE phone_key = v_key;
        RETURN v_contact;
    END IF;

    -- This fallback keeps any direct legacy contact inserts compatible during
    -- rollout.  The migration guard proves the key identifies at most one row.
    SELECT id INTO v_contact
      FROM contacts
     WHERE canonical_contact_phone(phone_normalized) = v_key
     LIMIT 1;
    IF FOUND THEN
        UPDATE contacts
           SET phone_normalized = v_display,
               phone_raw = coalesce(phone_raw, p_raw),
               updated_at = now()
         WHERE id = v_contact;
        INSERT INTO contact_phone_numbers(
            contact_id, phone_key, phone_normalized, phone_raw, label, role,
            is_primary, active, created_by_user_id
        ) VALUES(
            v_contact, v_key, v_display, p_raw, 'Основной', 'customer',
            TRUE, TRUE, NULL
        ) ON CONFLICT (phone_key) DO UPDATE
              SET updated_at = now()
          RETURNING contact_id INTO v_contact;
        RETURN v_contact;
    END IF;

    INSERT INTO contacts(phone_normalized, phone_raw)
    VALUES(v_display, p_raw)
    RETURNING id INTO v_contact;
    INSERT INTO contact_phone_numbers(
        contact_id, phone_key, phone_normalized, phone_raw, label, role,
        is_primary, active
    ) VALUES(
        v_contact, v_key, v_display, p_raw, 'Основной', 'customer', TRUE, TRUE
    ) ON CONFLICT (phone_key) DO UPDATE
          SET updated_at = now()
      RETURNING contact_id INTO v_contact;
    RETURN v_contact;
END $$;

-- The old function is redefined here rather than changing migration 002, so a
-- fresh database and an upgraded database share one deterministic resolver.
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
    v_raw_phone TEXT;
    v_phone_key TEXT;
    v_contact UUID;
    v_contact_phone_number UUID;
    v_call BIGINT;
BEGIN
    IF coalesce(p_duration_sec, 0) <= 0 OR p_direction NOT IN ('in', 'out') THEN
        RETURN NULL;
    END IF;
    v_raw_phone := CASE WHEN p_direction = 'in' THEN p_caller ELSE p_callee END;
    v_phone_key := canonical_contact_phone(v_raw_phone);
    -- Preserve the original ingestion contract for withheld/short/invalid
    -- remote values: persist the call and its recordings without inventing a
    -- contact.  Only a valid canonical phone gets identity resolution.
    IF v_phone_key IS NOT NULL THEN
        v_contact := contact_for_phone(v_raw_phone, v_raw_phone);
        SELECT id INTO v_contact_phone_number
          FROM contact_phone_numbers
         WHERE contact_id = v_contact AND phone_key = v_phone_key;
    END IF;

    INSERT INTO calls(
        call_id, source, external_call_id, direction, caller_number, callee_number,
        contact_id, contact_phone_number_id, started_at, ended_at, duration_sec,
        recording_in, recording_out, processing_status, status
    ) VALUES(
        p_external_call_id, 'asterisk', p_external_call_id, p_direction,
        p_caller, p_callee, v_contact, v_contact_phone_number, p_started_at,
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

COMMIT;
