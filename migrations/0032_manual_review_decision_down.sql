-- Reverte a auditoria da rota de saída de manual_review (item 1.3).
-- As decisões registradas em linhas existentes são descartadas junto com
-- as colunas. Restringido a baianas_moda_feminina; idempotente.

ALTER TABLE baianas_moda_feminina.orders
    DROP CONSTRAINT IF EXISTS ck_orders_manual_review_decision;
ALTER TABLE baianas_moda_feminina.orders
    DROP CONSTRAINT IF EXISTS orders_manual_review_decided_by_user_id_fkey;

ALTER TABLE baianas_moda_feminina.orders
    DROP COLUMN IF EXISTS manual_review_decided_reason;
ALTER TABLE baianas_moda_feminina.orders
    DROP COLUMN IF EXISTS manual_review_decided_by_user_id;
ALTER TABLE baianas_moda_feminina.orders
    DROP COLUMN IF EXISTS manual_review_decided_at;
ALTER TABLE baianas_moda_feminina.orders
    DROP COLUMN IF EXISTS manual_review_decision;
