-- Reverte 0038: remove o espelho do estoque informado pelo fornecedor.
-- Perde o teto da liberação de reserva (volta a devolver sem limite).
-- Restringido a baianas_moda_feminina; idempotente.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE product_variants
    DROP COLUMN IF EXISTS supplier_stock;
