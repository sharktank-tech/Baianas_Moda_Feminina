-- Migration: coluna de reserva de estoque (baianas_moda_feminina)
-- Aplicação: execute uma única vez no banco de produção (PostgreSQL):
--
--   ALTER TABLE orders ADD COLUMN stock_reserved BOOLEAN NOT NULL DEFAULT FALSE;
--
-- O app também cria a coluna automaticamente no primeiro boot (loja/schema.py),
-- mas rodar este SQL antecipadamente evita o primeiro erro até o redeploy.

ALTER TABLE orders ADD COLUMN stock_reserved BOOLEAN NOT NULL DEFAULT FALSE;