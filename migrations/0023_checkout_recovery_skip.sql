-- Descarte administrativo auditável de candidatos de recuperação.
-- Não representa envio, pagamento, cancelamento nem liberação de estoque.
BEGIN;
SET LOCAL search_path TO baianas_moda_feminina;

DO $$
BEGIN
    IF current_schema() IS DISTINCT FROM 'baianas_moda_feminina'
       OR to_regclass('baianas_moda_feminina.orders') IS NULL THEN
        RAISE EXCEPTION 'dedicated baianas_moda_feminina.orders required';
    END IF;
END
$$;

ALTER TABLE baianas_moda_feminina.orders
    ADD COLUMN IF NOT EXISTS recovery_email_skipped_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS recovery_email_skipped_by_user_id UUID,
    ADD COLUMN IF NOT EXISTS recovery_email_skip_reason VARCHAR(32);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'orders_recovery_email_skipped_by_user_id_fkey'
           AND conrelid = 'baianas_moda_feminina.orders'::regclass
    ) THEN
        ALTER TABLE baianas_moda_feminina.orders
            ADD CONSTRAINT orders_recovery_email_skipped_by_user_id_fkey
            FOREIGN KEY (recovery_email_skipped_by_user_id)
            REFERENCES baianas_moda_feminina.users(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'ck_orders_recovery_email_skip_reason'
           AND conrelid = 'baianas_moda_feminina.orders'::regclass
    ) THEN
        ALTER TABLE baianas_moda_feminina.orders
            ADD CONSTRAINT ck_orders_recovery_email_skip_reason
            CHECK (
                recovery_email_skip_reason IS NULL
                OR recovery_email_skip_reason = 'admin_decision'
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_orders_recovery_email_skipped_by_user_id
    ON baianas_moda_feminina.orders (recovery_email_skipped_by_user_id);

COMMIT;
