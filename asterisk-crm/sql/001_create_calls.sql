CREATE TABLE IF NOT EXISTS calls (
    id            BIGSERIAL PRIMARY KEY,
    call_id       TEXT UNIQUE NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    caller_number TEXT,
    callee_number TEXT,
    started_at    TIMESTAMPTZ NOT NULL,
    duration_sec  INTEGER,
    recording_in  TEXT,
    recording_out TEXT,
    transcript    TEXT,
    theme         TEXT,
    client_request TEXT,
    agreements    TEXT,
    amount        NUMERIC,
    next_step     TEXT,
    next_date     DATE,
    raw_json      JSONB,
    status        TEXT NOT NULL DEFAULT 'new' CHECK (
                    status IN ('new','transcribed','summarized','stored','failed')
                  ),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calls_started_at_idx ON calls (started_at DESC);
CREATE INDEX IF NOT EXISTS calls_status_idx     ON calls (status);
CREATE INDEX IF NOT EXISTS calls_caller_idx     ON calls (caller_number);
