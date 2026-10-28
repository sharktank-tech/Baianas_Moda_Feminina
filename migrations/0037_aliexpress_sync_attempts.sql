-- Tentativas de sincronização técnica de produto publicado (revisão PR #123).
--
-- A rota administrativa de sincronização gravava resultado só em flash de
-- sessão: tentativa e falha someam. Esta tabela persiste o RESULTADO de
-- cada tentativa (aplicada, recusada, falha inesperada ou erro de API) com
-- os códigos estáveis sinalizados (divergência de custo/moeda e completude
-- não provada do catálogo) e as contagens de estoque.
--
-- Nunca guarda payload do fornecedor, token, sku_id nem product_id externo.
-- Aditivo e idempotente; restrito ao schema `baianas_moda_feminina`.
-- DOWN em 0037_aliexpress_sync_attempts_down.sql.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

CREATE TABLE IF NOT EXISTS aliexpress_sync_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id uuid
        REFERENCES products(id) ON DELETE CASCADE,
    supplier_product_id uuid
        REFERENCES supplier_products(id) ON DELETE SET NULL,
    status varchar(16) NOT NULL,
    code varchar(64),
    divergence varchar(64),
    catalog_code varchar(64),
    variants_updated integer NOT NULL DEFAULT 0,
    variants_deactivated integer NOT NULL DEFAULT 0,
    variants_created integer NOT NULL DEFAULT 0,
    stock_before integer,
    stock_after integer,
    created_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_aliexpress_sync_attempt_status
        CHECK (status IN ('applied', 'rejected', 'failed', 'api_error'))
);

CREATE INDEX IF NOT EXISTS ix_aliexpress_sync_attempts_product_created
    ON aliexpress_sync_attempts (product_id, created_at);
