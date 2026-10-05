-- Cobertura explícita da confirmação coletiva de origem (revisão PR #123).
--
-- A linha de cobertura total (covers_all_variants) passa a registrar QUAIS
-- SKUs ela cobre, no momento da escrita. A resolução só aplica a cobertura
-- a esses SKUs: um SKU nascido depois nunca herda origem brasileira
-- sozinho e continua desconhecido (fail-closed) até ganhar escrita própria.
--
-- NULL = nada coberto (a coluna é preenchida junto com a confirmação/observação).
-- Aditivo e idempotente; restrito ao schema `pollomodas`.
-- DOWN em 0036_shipping_origin_cover_snapshot_down.sql.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE supplier_sku_shipping_origins
    ADD COLUMN IF NOT EXISTS covered_supplier_sku_ids TEXT;
