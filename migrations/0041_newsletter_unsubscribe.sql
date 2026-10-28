-- Migration 0041: opt-out da newsletter (cancelamento + reativação).
--
-- Aditivo e idempotente; restrito ao schema `baianas_moda_feminina`.
-- - unsubscribed_at: NULL = ativo; preenchido = cancelado (supressão).
-- - resubscribed_at: marca reativação explícita posterior (auditoria).
--
-- O app também cria as colunas no boot (loja/schema.py: REQUIRED_COLUMNS);
-- aplicar este SQL antecipadamente evita o primeiro erro até o redeploy.
--
-- DOWN em 0041_newsletter_unsubscribe_down.sql.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE newsletter_subscribers
    ADD COLUMN IF NOT EXISTS unsubscribed_at TIMESTAMP;

ALTER TABLE newsletter_subscribers
    ADD COLUMN IF NOT EXISTS resubscribed_at TIMESTAMP;
