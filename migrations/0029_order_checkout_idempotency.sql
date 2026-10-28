-- Idempotência do checkout (item 1.7): impede pedido duplicado quando o
-- cliente reenvia o form (duplo clique, F5). O cliente gera uma chave por
-- renderização da página de checkout; a constraint UNIQUE impede o 2º INSERT
-- e o endpoint redireciona para o pedido já criado.
-- Idempotente. DOWN em 0029_order_checkout_idempotency_down.sql.

ALTER TABLE baianas_moda_feminina.orders
    ADD COLUMN IF NOT EXISTS idempotency_key varchar(64);

-- Legados (NULL) nunca colidem: NULL <> NULL em comparação de unicidade.
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_user_idempotency_key
    ON baianas_moda_feminina.orders (user_id, idempotency_key);
