-- Reverte 0037: remove o histórico persistido de tentativas de sincronização.
-- Perde o auditoria de tentativas/falhas (voltam a existir só em flash).
-- Restringido a pollomodas; idempotente.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

DROP INDEX IF EXISTS ix_aliexpress_sync_attempts_product_created;
DROP TABLE IF EXISTS aliexpress_sync_attempts;
