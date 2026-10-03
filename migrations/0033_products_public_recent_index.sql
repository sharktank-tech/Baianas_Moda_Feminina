-- Índice do list público da home (PR #103/TTFB): filtro
-- is_active/is_draft + ordenação por created_at, usado também pela janela
-- de categorias (row_number por category ordenado por created_at).
--
-- create_all() não altera tabelas já existentes — esta migration é o
-- caminho para instalações provisionadas. Idempotente; reaplicar não
-- duplica. Para tabelas grandes em produção, considerar CREATE INDEX
-- CONCURRENTLY fora de transação.

CREATE INDEX IF NOT EXISTS ix_products_public_recent
    ON pollomodas.products (is_active, is_draft, created_at);
