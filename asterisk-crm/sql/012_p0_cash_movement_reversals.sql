-- Confirmed movements remain immutable.  A correction is another posted row
-- which explicitly references the row it compensates.

ALTER TABLE deal_cash_movements
    ADD COLUMN IF NOT EXISTS reversal_of_movement_id UUID;

DO $$ BEGIN
    ALTER TABLE deal_cash_movements
        ADD CONSTRAINT deal_cash_movements_reversal_of_movement_fk
        FOREIGN KEY (reversal_of_movement_id) REFERENCES deal_cash_movements(id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DROP INDEX IF EXISTS deal_cash_movements_one_reversal_idx;
CREATE UNIQUE INDEX deal_cash_movements_one_reversal_idx
    ON deal_cash_movements(reversal_of_movement_id)
    WHERE reversal_of_movement_id IS NOT NULL;

ALTER TABLE deal_cash_movements
    DROP CONSTRAINT IF EXISTS deal_cash_movements_kind_check;
ALTER TABLE deal_cash_movements
    ADD CONSTRAINT deal_cash_movements_kind_check CHECK (kind IN (
        'customer_incoming', 'customer_refund',
        'realized_cost_outflow', 'realized_cost_outflow_reversal',
        'other_reserved_cash', 'other_reserved_cash_release'
    ));
