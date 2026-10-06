-- Estoque total informado pelo fornecedor por variante (revisão PR #123).
--
-- A liberação de reserva devolvia "estoque local + quantidade" sem teto:
-- quando o fornecedor REDUZIU o estoque depois da sincronização (que já
-- gravou max(S - reservas, 0)), a liberação inflava o estoque local ACIMA
-- do que o fornecedor ainda tem — estoque fantasma vendável.
--
-- Esta coluna espelha o total informado na última sincronização técnica e
-- serve de TETO: a liberação nunca devolve acima de
-- (supplier_stock - outras reservas ainda debitadas).
--
-- NULL = nunca sincronizado (a liberação segue sem teto, como antes).
-- Sem backfill: só a próxima sincronização técnica preenche.
-- Aditivo e idempotente; restrito ao schema `pollomodas`.
-- DOWN em 0038_variant_supplier_stock_down.sql.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE product_variants
    ADD COLUMN IF NOT EXISTS supplier_stock integer;
