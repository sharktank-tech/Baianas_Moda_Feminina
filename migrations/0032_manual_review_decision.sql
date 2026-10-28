-- Item 1.3: auditoria da rota de saída de manual_review.
-- Quem decidiu (aprovar/rejeitar), quando, com qual nota — na própria linha
-- do pedido, mesmo precedente de recovery_email_skipped_* (0023). Nenhuma
-- mudança de comportamento: colunas anuláveis; código antigo as ignora.
-- Idempotente. DOWN em 0032_manual_review_decision_down.sql.

ALTER TABLE baianas_moda_feminina.orders
    ADD COLUMN IF NOT EXISTS manual_review_decision VARCHAR(16),
    ADD COLUMN IF NOT EXISTS manual_review_decided_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS manual_review_decided_by_user_id UUID,
    ADD COLUMN IF NOT EXISTS manual_review_decided_reason TEXT;

ALTER TABLE baianas_moda_feminina.orders
    DROP CONSTRAINT IF EXISTS ck_orders_manual_review_decision;
ALTER TABLE baianas_moda_feminina.orders
    ADD CONSTRAINT ck_orders_manual_review_decision
    CHECK (
        manual_review_decision IS NULL
        OR manual_review_decision IN ('approved', 'rejected')
    );

ALTER TABLE baianas_moda_feminina.orders
    DROP CONSTRAINT IF EXISTS orders_manual_review_decided_by_user_id_fkey;
ALTER TABLE baianas_moda_feminina.orders
    ADD CONSTRAINT orders_manual_review_decided_by_user_id_fkey
    FOREIGN KEY (manual_review_decided_by_user_id)
    REFERENCES baianas_moda_feminina.users(id)
    ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_orders_manual_review_decided_by_user_id
    ON baianas_moda_feminina.orders (manual_review_decided_by_user_id);
