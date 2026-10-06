-- Reverte 0036: remove o snapshot de SKUs cobertos pela confirmação coletiva.
--
-- ATENÇÃO: sem a coluna a cobertura total volta a valer para qualquer SKU
-- (inclusive os nascidos depois da confirmação). Restringido a pollomodas;
-- idempotente.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE supplier_sku_shipping_origins
    DROP COLUMN IF EXISTS covered_supplier_sku_ids;
