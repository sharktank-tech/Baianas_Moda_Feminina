-- Migration 0004: import de rascunho do catálogo AliExpress (FASE 5) — aditiva.
--
-- Escopo e histórico:
--   * 0001 (create schema)  -> instalação NOVA: cria o schema `baianas_moda_feminina`.
--   * 0003 (UUID normalize) -> UPGRADE de instalação legada (VARCHAR -> UUID).
--   * 0004 (esta)           -> colunas de frete em supplier_products + atributos
--     JSONB em product_variants, necessárias ao import idempotente de drafts.
--
-- Estratégia: APENAS adições (ADD COLUMN IF NOT EXISTS + CHECK guardado por
-- pg_constraint). Nunca destrutiva, nunca toca `public`. É idempotente: pode
-- ser executada mais de uma vez sem erro.
--
-- Aplicação: execute UMA OU MAIS VEZES no banco alvo (PostgreSQL):
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0004_aliexpress_draft_import.sql
--
-- Observações:
--   * supplier_products.shipping_cost / landed_cost são NULLABLE de propósito:
--     ausência de frete NUNCA vira zero (freight_status='provider_unavailable').
--   * product_variants.attributes guarda o dict de atributos do SKU AliExpress
--     (persistência fiel; color/size continuam sendo conveniência).
--   * Esta migration também é reexecutável pelo teste de validação em schema
--     descartável (pollo_test_*); o app atualiza as mesmas colunas via
--     ensure_schema() para bancos que não rodaram o arquivo.

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da baianas_moda_feminina. O schema
-- `public` (legado) jamais deve ser tocado por esta migration.
SET search_path TO baianas_moda_feminina;

ALTER TABLE supplier_products
    ADD COLUMN IF NOT EXISTS freight_status VARCHAR(30) NOT NULL DEFAULT 'unknown';

ALTER TABLE supplier_products
    ADD COLUMN IF NOT EXISTS shipping_cost NUMERIC(10, 2);

ALTER TABLE supplier_products
    ADD COLUMN IF NOT EXISTS landed_cost NUMERIC(10, 2);

ALTER TABLE supplier_products
    ADD COLUMN IF NOT EXISTS freight_currency VARCHAR(3);

ALTER TABLE product_variants
    ADD COLUMN IF NOT EXISTS attributes JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_supplier_product_shipping_cost_nonneg'
          AND conrelid = 'baianas_moda_feminina.supplier_products'::regclass
    ) THEN
        ALTER TABLE supplier_products
            ADD CONSTRAINT ck_supplier_product_shipping_cost_nonneg
            CHECK (shipping_cost IS NULL OR shipping_cost >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_supplier_product_landed_cost_nonneg'
          AND conrelid = 'baianas_moda_feminina.supplier_products'::regclass
    ) THEN
        ALTER TABLE supplier_products
            ADD CONSTRAINT ck_supplier_product_landed_cost_nonneg
            CHECK (landed_cost IS NULL OR landed_cost >= 0);
    END IF;
END $$;