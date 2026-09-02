-- Migration 0010: canonical AliExpress place-order snapshot (Fase 8.0.10)
-- Idempotente, restrita ao schema `pollomodas`. Nunca toca `public`.
--
-- Cria uma tabela 1:1 por aliexpress_orders para congelar os dados de escrita
-- exigidos pelo futuro `aliexpress.trade.buy.placeorder`:
--   * shipping_address JSONB (PII interno, nunca exposto em admin/logs);
--   * items JSONB (product_id, sku_attr, product_count, logistics_service_name).
--
-- Esta migration NÃO habilita HTTP nem altera estados de fulfillment.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

CREATE TABLE IF NOT EXISTS aliexpress_submission_snapshots (
    id UUID PRIMARY KEY NOT NULL,
    aliexpress_order_id UUID NOT NULL,
    shipping_address JSONB NOT NULL,
    items JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_aliexpress_submission_snapshot_order UNIQUE (aliexpress_order_id),
    CONSTRAINT fk_aliexpress_submission_snapshot_order
        FOREIGN KEY (aliexpress_order_id)
        REFERENCES aliexpress_orders(id)
        ON DELETE CASCADE,
    CONSTRAINT ck_aliexpress_submission_snapshot_address_object
        CHECK (jsonb_typeof(shipping_address) = 'object'),
    CONSTRAINT ck_aliexpress_submission_snapshot_items_array
        CHECK (jsonb_typeof(items) = 'array' AND jsonb_array_length(items) > 0)
);

CREATE INDEX IF NOT EXISTS ix_aliexpress_submission_snapshots_order_id
    ON aliexpress_submission_snapshots(aliexpress_order_id);

-- Defesa: objetos novos da integração permanecem proibidos em public.
DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
          'aliexpress_credentials',
          'aliexpress_order_items',
          'aliexpress_submission_snapshots'
      );
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0010 criou objetos novos no schema public (proibido)';
    END IF;
END $$;
