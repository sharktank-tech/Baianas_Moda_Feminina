-- Registro manual de compra no fornecedor e despacho (sem automação).
--
-- A interface registra compras JÁ realizadas pelo operador: salvar o
-- formulário nunca compra no AliExpress nem consulta o fornecedor. Por isso
-- NÃO reutiliza `aliexpress_orders` (máquina de fulfillment automático com
-- estados queued/blocked/ready/submitting/...): preencher aqueles estados
-- só para caber o registro manual corromperia a semântica da automação.
--
-- Tabelas novas (aditivas, idempotentes, restritas ao schema `pollomodas`):
-- - manual_supplier_purchases: uma linha por pedido AliExpress (compras
--   divididas = várias linhas); external_order_id UNIQUE impede o mesmo
--   número em dois pedidos e dá idempotência sob concorrência.
-- - manual_supplier_purchase_items: quantidades por item da loja (teto
--   aplicado na camada de serviço sob lock; sem constraint cross-table).
-- - shipments.manual_purchase_id: vínculo opcional remessa→compra.
-- - shipment_items: cobertura item/quantidade por remessa.
-- - manual_operational_audit: correções com antes/depois, motivo e autor;
--   sem exclusão física pela interface.
--
-- DOWN em 0039_manual_dispatch_down.sql.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

CREATE TABLE IF NOT EXISTS manual_supplier_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    external_order_id VARCHAR(64) NOT NULL UNIQUE,
    purchased_at TIMESTAMP NOT NULL,
    purchased_tz VARCHAR(64) NOT NULL DEFAULT 'America/Sao_Paulo',
    evidence_ref VARCHAR(255) NOT NULL,
    note TEXT,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_manual_purchases_order
    ON manual_supplier_purchases(order_id);

CREATE TABLE IF NOT EXISTS manual_supplier_purchase_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_id UUID NOT NULL
        REFERENCES manual_supplier_purchases(id) ON DELETE CASCADE,
    order_item_id UUID NOT NULL REFERENCES order_items(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE(purchase_id, order_item_id)
);

CREATE INDEX IF NOT EXISTS ix_manual_purchase_items_purchase
    ON manual_supplier_purchase_items(purchase_id);

ALTER TABLE shipments
    ADD COLUMN IF NOT EXISTS manual_purchase_id UUID
        REFERENCES manual_supplier_purchases(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_shipments_manual_purchase
    ON shipments(manual_purchase_id);

CREATE TABLE IF NOT EXISTS shipment_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_id BigInteger NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    order_item_id UUID NOT NULL REFERENCES order_items(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE(shipment_id, order_item_id)
);

CREATE INDEX IF NOT EXISTS ix_shipment_items_shipment
    ON shipment_items(shipment_id);

CREATE TABLE IF NOT EXISTS manual_operational_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity VARCHAR(16) NOT NULL CHECK (entity IN ('purchase', 'shipment')),
    entity_id TEXT NOT NULL,
    field VARCHAR(64) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    admin_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reason VARCHAR(500) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_manual_audit_entity
    ON manual_operational_audit(entity, entity_id);
