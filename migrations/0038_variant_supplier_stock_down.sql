-- Reverte 0038: remove o espelho do estoque informado pelo fornecedor.
-- Perde o teto da liberação de reserva (volta a devolver sem limite).
-- Restringido a pollomodas; idempotente.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE product_variants
    DROP COLUMN IF EXISTS supplier_stock;
