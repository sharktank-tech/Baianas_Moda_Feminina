-- Migration 0007: AliExpress Fulfillment Core (Fase 8.0 / 8.0.1)
-- — Idempotente, restrita ao schema `pollomodas`. Nunca toca `public`.
--
-- Contexto:
--   * 0005 -> precificação/publicação (products)
--   * 0006 -> storefront multivariante (cart/orders variant-aware)
--   * 0007 (esta) -> pipeline de fulfillment AliExpress:
--       - credencial persistente criptografada (Fernet)
--       - AliExpressOrder evoluído (novos estados, supplier_id, timestamps)
--       - AliExpressOrderItem (snapshot de SKU do fornecedor no momento do fulfillment)
--       - índices/constraints de idempotência
--
-- Estados canônicos de AliExpressOrder (novos):
--   queued, blocked, ready, submitting, submitted, confirmed,
--   shipped, delivered, cancel_requested, cancelled, failed, manual_review
--   (compatível com state="pending" legado)
--
-- Fase 8.0.1 (ajustes neste arquivo):
--   * Colunas de data/hora em TIMESTAMP WITHOUT TIME ZONE (o projeto usa
--     utcnow() UTC naive + db.DateTime sem timezone — nunca TIMESTAMPTZ,
--     para evitar mistura aware/naive).
--   * state padrão de AliExpressOrder = 'queued' (legado 'pending' continua
--     aceito pelo CHECK de estado).
--   * AliExpressOrderItem.product_id NULLABLE com FK ON DELETE SET NULL:
--     a exclusão do Product não apaga o snapshot do item.
--   * Checks de estado (ck_aliexpress_order_state /
--     ck_aliexpress_order_item_state) idempotentes e com EXATAMENTE os
--     estados do ORM.
--
-- Aplicação (uma ou mais vezes, PostgreSQL):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0007_aliexpress_fulfillment_core.sql
--
-- Observação de segurança: NENHUM dado sensível (token, secret, SKU externo)
-- aparece em índices ou logs. raw_payload permanece NULL ou apenas resumo sanitizado.

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da PolloModas.
SET search_path TO pollomodas;

-- ============================================================
-- 1) Tabela de credenciais persistentes criptografadas
-- ============================================================
CREATE TABLE IF NOT EXISTS aliexpress_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    seller_id VARCHAR(64) NOT NULL,
    access_token_encrypted TEXT NOT NULL,
    refresh_token_encrypted TEXT,
    access_token_expires_at TIMESTAMP,
    refresh_token_expires_at TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_aliexpress_credential_supplier UNIQUE (supplier_id),
    CONSTRAINT ck_aliexpress_credential_status CHECK (
        status IN ('active', 'expired', 'invalid', 'disabled')
    )
);

CREATE INDEX IF NOT EXISTS ix_aliexpress_credential_seller
    ON aliexpress_credentials (seller_id);

-- ============================================================
-- 2) Evoluir aliexpress_orders (aditivo, idempotente)
-- ============================================================
ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS supplier_id UUID REFERENCES suppliers(id) ON DELETE SET NULL;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS queued_at TIMESTAMP NOT NULL DEFAULT now();

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP;

-- Padrão de state: 'queued' (novo). 'pending' legado continua aceito pelo CHECK.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'pollomodas'
          AND table_name = 'aliexpress_orders'
          AND column_name = 'state'
    ) THEN
        EXECUTE 'ALTER TABLE aliexpress_orders ALTER COLUMN state SET DEFAULT ''queued''';
    END IF;
END $$;

-- Constraint parcial de unicidade: (order_id, supplier_id) quando supplier_id NOT NULL
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'pollomodas'
          AND tablename = 'aliexpress_orders'
          AND indexname = 'uq_aliexpress_order_supplier'
    ) THEN
        CREATE UNIQUE INDEX uq_aliexpress_order_supplier
            ON aliexpress_orders (order_id, supplier_id)
            WHERE supplier_id IS NOT NULL;
    END IF;
END $$;

-- Índice para queries por supplier
CREATE INDEX IF NOT EXISTS ix_aliexpress_orders_supplier
    ON aliexpress_orders (supplier_id);

-- Índice para fila administrativa (estado + updated_at)
CREATE INDEX IF NOT EXISTS ix_aliexpress_orders_state_updated
    ON aliexpress_orders (state, updated_at);

-- Índice para idempotency_key (já existe UNIQUE na tabela)
-- Garantir que o índice existe
CREATE INDEX IF NOT EXISTS ix_aliexpress_orders_idempotency
    ON aliexpress_orders (idempotency_key);

-- Check de estado de AliExpressOrder (idempotente; EXATAMENTE estados do ORM
-- + 'pending' legado permitido).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_aliexpress_order_state'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'pollomodas' AND c.relname = 'aliexpress_orders'
          )
    ) THEN
        ALTER TABLE aliexpress_orders
            ADD CONSTRAINT ck_aliexpress_order_state CHECK (
                state IN (
                    'queued', 'blocked', 'ready', 'submitting', 'submitted',
                    'confirmed', 'shipped', 'delivered', 'cancel_requested',
                    'cancelled', 'failed', 'manual_review', 'pending'
                )
            );
    END IF;
END $$;

-- ============================================================
-- 3) Nova tabela: AliExpressOrderItem (snapshot do SKU comprado)
-- ============================================================
CREATE TABLE IF NOT EXISTS aliexpress_order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aliexpress_order_id UUID NOT NULL REFERENCES aliexpress_orders(id) ON DELETE CASCADE,
    order_item_id UUID NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    product_id UUID REFERENCES products(id) ON DELETE SET NULL,
    variant_id UUID REFERENCES product_variants(id) ON DELETE SET NULL,
    supplier_product_external_id VARCHAR(64) NOT NULL,
    supplier_sku_external_id VARCHAR(64) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    supplier_unit_cost NUMERIC(10,2),
    supplier_currency VARCHAR(3),
    variant_label VARCHAR(255),
    state VARCHAR(20) NOT NULL DEFAULT 'queued',
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_aliexpress_order_item_order_item UNIQUE (order_item_id)
);

-- Em schema pré-existente (criado por versão anterior da 0007), o product_id
-- pode ainda estar NOT NULL + ON DELETE CASCADE. Idempotente: remove o NOT NULL
-- (o snapshot consegue sobreviver à exclusão do Product mesmo mantendo a FK).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'pollomodas'
          AND table_name = 'aliexpress_order_items'
          AND column_name = 'product_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE aliexpress_order_items ALTER COLUMN product_id DROP NOT NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_aliexpress_order_items_ali_order
    ON aliexpress_order_items (aliexpress_order_id);

CREATE INDEX IF NOT EXISTS ix_aliexpress_order_items_order_item
    ON aliexpress_order_items (order_item_id);

CREATE INDEX IF NOT EXISTS ix_aliexpress_order_items_product
    ON aliexpress_order_items (product_id);

-- Check de estado de AliExpressOrderItem (idempotente; EXATAMENTE estados do ORM).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_aliexpress_order_item_state'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'pollomodas' AND c.relname = 'aliexpress_order_items'
          )
    ) THEN
        ALTER TABLE aliexpress_order_items
            ADD CONSTRAINT ck_aliexpress_order_item_state CHECK (
                state IN (
                    'queued', 'blocked', 'ready', 'submitting', 'submitted',
                    'confirmed', 'shipped', 'delivered', 'cancel_requested',
                    'cancelled', 'failed', 'manual_review'
                )
            );
    END IF;
END $$;

-- ============================================================
-- 4) Helper: função para gerar idempotency_key determinística
-- ============================================================
-- Usada pela aplicação (não pelo banco), mas documentada aqui:
-- idempotency_key = sha256('fulfillment:v1:' || order_id || ':' || supplier_id)
-- Em SQL puro, se necessário:
-- CREATE OR REPLACE FUNCTION _gen_fulfillment_idempotency_key(p_order_id UUID, p_supplier_id UUID)
-- RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
--   SELECT encode(digest('fulfillment:v1:' || p_order_id || ':' || p_supplier_id, 'sha256'), 'hex')
-- $$;

-- ============================================================
-- Verificação de schema public invariável
-- ============================================================
-- Esta migration NÃO deve criar objetos no schema public.
-- Verificação rápida (falha se houver objetos criados em public por engano):
DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('aliexpress_credentials', 'aliexpress_order_items');
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0007 criou objetos no schema public (proibido)';
    END IF;
END $$;