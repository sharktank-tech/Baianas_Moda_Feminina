-- Reverte a auditoria da rota de saída de manual_review (item 1.3).
-- As decisões registradas em linhas existentes são descartadas junto com
-- as colunas. Restringido a pollomodas; idempotente.

ALTER TABLE pollomodas.orders
    DROP CONSTRAINT IF EXISTS ck_orders_manual_review_decision;
ALTER TABLE pollomodas.orders
    DROP CONSTRAINT IF EXISTS orders_manual_review_decided_by_user_id_fkey;

ALTER TABLE pollomodas.orders
    DROP COLUMN IF EXISTS manual_review_decided_reason;
ALTER TABLE pollomodas.orders
    DROP COLUMN IF EXISTS manual_review_decided_by_user_id;
ALTER TABLE pollomodas.orders
    DROP COLUMN IF EXISTS manual_review_decided_at;
ALTER TABLE pollomodas.orders
    DROP COLUMN IF EXISTS manual_review_decision;
