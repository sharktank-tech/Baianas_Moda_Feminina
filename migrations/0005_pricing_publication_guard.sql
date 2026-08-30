-- Migration 0005: precificação dropshipping + guarda de publicação (FASE 6)
-- — APENAS adições, restritas ao schema `pollomodas`. Nunca toca `public`.
--
-- Escopo e histórico:
--   * 0004 (draft import) -> colunas de frete em supplier_products.
--   * 0005 (esta)         -> colunas de precificação em products + defesa de
--     banco que impede `is_active=true` em AliExpress não precificado.
--
-- Estratégia: APENAS adições (ADD COLUMN IF NOT EXISTS + CHECK guardado por
-- pg_constraint via conname + conrelid). Nunca destrutiva, nunca toca `public`.
-- É idempotente: pode ser executada mais de uma vez sem erro.
--
-- Aplicação (uma ou mais vezes, PostgreSQL):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0005_pricing_publication_guard.sql
--
-- Estados permitidos de products.pricing_status (string):
--   unpriced | blocked | ready | priced | stale
--
-- CHECK ck_product_target_margin_range:
--   target_margin_percent NÃO pode ser 0, 100, negativo nem >= 100. NULL é
--   permitido e significa "produto ainda não precificável/publicável".
--
-- CHECK ck_product_aliexpress_publication_guard:
--   Um produto AliExpress só pode ficar ATIVO se NÃO for rascunho E estiver
--   priced. Isso não substitui evaluate_publication() da aplicação — impede
--   apenas que um simples `is_active=true` publique um AliExpress não
--   precificado.
--
-- Observações:
--   * `currency` usa DEFAULT 'BRL': produtos internos/legados continuam BRL.
--   * Não há margem padrão: target_margin_percent precisa ser fornecida.
--   * Esta migration também é reexecutável pelos testes em schema descartável
--     (pollo_test_pricing_*); o app atualiza as mesmas colunas via
--     ensure_schema() para bancos que não rodaram o arquivo.

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da PolloModas. O schema
-- `public` (legado) jamais deve ser tocado por esta migration.
SET search_path TO pollomodas;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS currency VARCHAR(3)
        NOT NULL DEFAULT 'BRL';

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS target_margin_percent NUMERIC(5, 2);

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS pricing_status VARCHAR(20)
        NOT NULL DEFAULT 'unpriced';

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS pricing_error TEXT;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS pricing_updated_at TIMESTAMP;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_product_target_margin_range'
          AND conrelid = 'pollomodas.products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_product_target_margin_range
            CHECK (
                target_margin_percent IS NULL
                OR (
                    target_margin_percent > 0
                    AND target_margin_percent < 100
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_product_aliexpress_publication_guard'
          AND conrelid = 'pollomodas.products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_product_aliexpress_publication_guard
            CHECK (
                source != 'aliexpress'
                OR is_active = false
                OR (
                    is_draft = false
                    AND pricing_status = 'priced'
                )
            );
    END IF;
END $$;