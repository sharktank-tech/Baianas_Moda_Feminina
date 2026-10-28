-- Reverte 0039: remove o registro manual de compra/despacho.
-- Perde compras manuais, coberturas de remessa e auditoria operacional.
-- Pedidos, itens, reservas, pagamentos e remessas sem vínculo manual
-- permanecem intactos. Restrito a baianas_moda_feminina; idempotente.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

DROP TABLE IF EXISTS manual_operational_audit;
DROP TABLE IF EXISTS shipment_items;
ALTER TABLE shipments DROP COLUMN IF EXISTS manual_purchase_id;
DROP TABLE IF EXISTS manual_supplier_purchase_items;
DROP TABLE IF EXISTS manual_supplier_purchases;
