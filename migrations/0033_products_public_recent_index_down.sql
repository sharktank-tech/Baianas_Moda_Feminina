-- Reverte o índice da home (PR #103). Sem perda de dado; o list público
-- volta a seq scan. Idempotente.

DROP INDEX IF EXISTS baianas_moda_feminina.ix_products_public_recent;
