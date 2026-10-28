-- Reverte 0041: remove as colunas de opt-out da newsletter.
-- Perde o histórico de cancelamento/reativação (inscritos ficam ativos).
-- Restrito a baianas_moda_feminina; idempotente.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE newsletter_subscribers
    DROP COLUMN IF EXISTS resubscribed_at;

ALTER TABLE newsletter_subscribers
    DROP COLUMN IF EXISTS unsubscribed_at;
