-- Migration 0013: audited AliExpress submission snapshot revisions (FASE 8.0.11Y)
-- Idempotente, restrita ao schema `baianas_moda_feminina`. Nunca toca `public`.
--
-- Objetivo:
--   preservar integralmente o snapshot ativo ANTES de uma correção manual e
--   controlada de shipping_address. A tabela é somente histórico/auditoria;
--   não habilita HTTP, retry, submissão nem altera estado de fulfillment.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

CREATE TABLE IF NOT EXISTS aliexpress_submission_snapshot_revisions (
    id UUID PRIMARY KEY NOT NULL,
    aliexpress_order_id UUID NOT NULL,
    snapshot_id UUID NULL,
    revision_no INTEGER NOT NULL,
    shipping_address JSONB NOT NULL,
    items JSONB NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    changed_fields JSONB NOT NULL,
    snapshot_created_at TIMESTAMP NULL,
    archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_aliexpress_snapshot_revision_order_no
        UNIQUE (aliexpress_order_id, revision_no),
    CONSTRAINT fk_aliexpress_snapshot_revision_order
        FOREIGN KEY (aliexpress_order_id)
        REFERENCES aliexpress_orders(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_aliexpress_snapshot_revision_snapshot
        FOREIGN KEY (snapshot_id)
        REFERENCES aliexpress_submission_snapshots(id)
        ON DELETE SET NULL,
    CONSTRAINT ck_aliexpress_snapshot_revision_no_positive
        CHECK (revision_no >= 1),
    CONSTRAINT ck_aliexpress_snapshot_revision_address_object
        CHECK (jsonb_typeof(shipping_address) = 'object'),
    CONSTRAINT ck_aliexpress_snapshot_revision_items_array
        CHECK (jsonb_typeof(items) = 'array' AND jsonb_array_length(items) > 0),
    CONSTRAINT ck_aliexpress_snapshot_revision_changed_fields_array
        CHECK (jsonb_typeof(changed_fields) = 'array' AND jsonb_array_length(changed_fields) > 0)
);

CREATE INDEX IF NOT EXISTS ix_aliexpress_snapshot_revisions_order_id
    ON aliexpress_submission_snapshot_revisions(aliexpress_order_id);

CREATE INDEX IF NOT EXISTS ix_aliexpress_snapshot_revisions_snapshot_id
    ON aliexpress_submission_snapshot_revisions(snapshot_id);

DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = 'aliexpress_submission_snapshot_revisions';
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0013 criou objeto novo no schema public (proibido)';
    END IF;
END $$;
