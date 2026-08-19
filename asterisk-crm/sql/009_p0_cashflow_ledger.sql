-- Confirmed cash is append-only.  Corrections are compensating movements,
-- never edits or deletes of a posted movement.

CREATE TABLE IF NOT EXISTS deal_cost_obligations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID NOT NULL REFERENCES deals(id) ON DELETE RESTRICT,
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    description TEXT,
    due_date DATE,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled', 'cancelled')),
    settled_at TIMESTAMPTZ,
    created_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deal_cash_movements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID NOT NULL REFERENCES deals(id) ON DELETE RESTRICT,
    obligation_id UUID REFERENCES deal_cost_obligations(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'customer_incoming', 'customer_refund', 'realized_cost_outflow',
        'other_reserved_cash', 'other_reserved_cash_release'
    )),
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ NOT NULL,
    note TEXT,
    created_by UUID REFERENCES crm_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE deal_cost_obligations
    ADD COLUMN IF NOT EXISTS settled_movement_id UUID;
DO $$ BEGIN
    ALTER TABLE deal_cost_obligations ADD CONSTRAINT deal_cost_obligations_settled_movement_fk
        FOREIGN KEY (settled_movement_id) REFERENCES deal_cash_movements(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS deal_cash_movements_deal_confirmed_idx
    ON deal_cash_movements(deal_id, confirmed_at DESC);
CREATE INDEX IF NOT EXISTS deal_cost_obligations_deal_open_idx
    ON deal_cost_obligations(deal_id, due_date) WHERE status = 'open';

CREATE OR REPLACE FUNCTION reject_deal_cash_movement_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'deal_cash_movements are append-only; create a compensating movement instead';
END $$;

DROP TRIGGER IF EXISTS deal_cash_movements_immutable ON deal_cash_movements;
CREATE TRIGGER deal_cash_movements_immutable
    BEFORE UPDATE OR DELETE ON deal_cash_movements
    FOR EACH ROW EXECUTE FUNCTION reject_deal_cash_movement_mutation();
