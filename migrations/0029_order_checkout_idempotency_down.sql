-- DOWN de 0029_order_checkout_idempotency.sql — idempotente.
-- Cobre os dois formatos possíveis do índice: constraint de tabela (create_all)
-- e índice único simples (como a migration cria em produção).
ALTER TABLE baianas_moda_feminina.orders
    DROP CONSTRAINT IF EXISTS uq_orders_user_idempotency_key;
DROP INDEX IF EXISTS baianas_moda_feminina.uq_orders_user_idempotency_key;
ALTER TABLE baianas_moda_feminina.orders DROP COLUMN IF EXISTS idempotency_key;
