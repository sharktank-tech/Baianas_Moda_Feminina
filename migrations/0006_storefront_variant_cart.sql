-- Migration 0006: storefront multivariante — carrinho/pedido variant-aware (FASE 7.2)
-- — Idempotente, restrita ao schema `baianas_moda_feminina`. Nunca toca `public`.
--
-- Contexto:
--   * 0005 -> precificação/publicação (products).
--   * 0006 (esta) -> o carrinho e os itens de pedido passam a suportar a opção
--     canônica (ProductVariant) para AliExpress.
--
-- Problema endereçado:
--   A antiga constraint literal `uq_cart_item_product_size
--   (cart_id, product_id, selected_size)` impedia duas variantes do mesmo
--   produto com o mesmo tamanho coexistirem no carrinho (ex.: "Rosa/M" e
--   "Azul/M" — mesmo product_id e selected_size="M"). O storefront escolhe a
--   variante por `variant_id` (UUID interno), nunca por supplier_sku_id.
--
-- Estratégia:
--   * ADITIVA e idempotente: ADD COLUMN IF NOT EXISTS + FK guardada por
--     pg_constraint + índices parciais via CREATE UNIQUE INDEX IF NOT EXISTS.
--   * Substitui a constraint literal por DOIS índices parciais mutuamente
--     exclusivos:
--       - uq_cart_item_variant        (cart_id, variant_id)
--                                             WHERE variant_id IS NOT NULL
--       - uq_cart_item_product_size   (cart_id, product_id, selected_size)
--                                             WHERE variant_id IS NULL
--   * Garante também variant_id + FK em order_items (snapshot do pedido).
--
-- Aplicação (uma ou mais vezes, PostgreSQL):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0006_storefront_variant_cart.sql
--
-- Observação de segurança: nenhum dado externo do AliExpress debaixo do
-- requirement (supplier_sku_id/external_sku_id) é utilizado como chave aqui.

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da baianas_moda_feminina.
SET search_path TO baianas_moda_feminina;

-- cart_items.variant_id (aditivo, idempotente)
ALTER TABLE cart_items
    ADD COLUMN IF NOT EXISTS variant_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'cart_items_variant_id_fkey'
          AND conrelid = 'baianas_moda_feminina.cart_items'::regclass
    ) THEN
        ALTER TABLE cart_items
            ADD CONSTRAINT cart_items_variant_id_fkey
            FOREIGN KEY (variant_id)
            REFERENCES product_variants(id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- order_items.variant_id (garantia idempotente, aditivo)
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS variant_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'order_items_variant_id_fkey'
          AND conrelid = 'baianas_moda_feminina.order_items'::regclass
    ) THEN
        ALTER TABLE order_items
            ADD CONSTRAINT order_items_variant_id_fkey
            FOREIGN KEY (variant_id)
            REFERENCES product_variants(id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- Substitui a constraint literal por índices parciais (DROP idempotente).
ALTER TABLE cart_items
    DROP CONSTRAINT IF EXISTS uq_cart_item_product_size;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cart_item_variant
    ON cart_items (cart_id, variant_id)
    WHERE variant_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cart_item_product_size
    ON cart_items (cart_id, product_id, selected_size)
    WHERE variant_id IS NULL;
