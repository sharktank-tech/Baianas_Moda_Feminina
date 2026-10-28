-- Migration 0003: normalização de IDs legados VARCHAR -> UUID (baianas_moda_feminina)
-- Estratégia 2 (conversão explícita, FORA do ensure_schema).
--
-- IMPORTANTE — ESCOPO E HISTÓRICO:
--   * 0001 (create schema)  -> instalação NOVA: cria o schema `baianas_moda_feminina`.
--   * 0003 (UUID normalize) -> UPGRADE de instalação LEGADA/preexistente que
--     ainda possua IDs VARCHAR. NÃO é necessária para um bootstrap limpo
--     (db.create_all() já produz UUID diretamente a partir dos models).
--   * Esta migration NUNCA deve atingir o schema `public` (sistema legado,
--     intocável). Por isso fixa `SET search_path TO baianas_moda_feminina;` abaixo; todas
--     as referências não-qualificadas resolvem para `baianas_moda_feminina`.
--   * Trate como ferramenta de RECUPERAÇÃO de instalação legada, não como
--     passo obrigatório de novos ambientes.
--
-- Pré-requisitos (audit já realizado):
--   - products, suppliers e order_items estão vazias (sem rows);
--   - nenhum valor inválido encontrado em products.id / suppliers.id;
--   - portanto o cast USING <coluna>::uuid é seguro para essas tabelas.
--   Atenção: cart_items.id / cart_items.product_id / order_items.id /
--   order_items.product_id também são convertidos; garanta que essas
--   colunas contenham SOMENTE UUIDs válidos (o cast aborta a transaction
--   caso contrário -> ROLLBACK, sem danos).
--
-- Aplicação: execute UMA VEZ no banco alvo (PostgreSQL), em janela de
-- manutenção e com backup prévio:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0003_normalize_legacy_uuid_ids.sql
--
-- Comportamento em erro: tudo roda dentro de uma transação. Se qualquer
-- cast falhar (valor não-UUID) ou uma FK não puder ser recriada, o
-- PostgreSQL aborta a transação e NENHUMA alteração é commitada
-- (equivalente a ROLLBACK). Ao ver um erro, rode ROLLBACK.
--
-- Não cria product_supplier manualmente: db.create_all() o recria (com FKs
-- UUID) quando os tipos dos pais já estiverem compatíveis. Caso product_supplier
-- já exista com colunas FK VARCHAR, sua FK é restaurada se possível; se não,
-- o create_all a recriará ao (re)criar a tabela.

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da baianas_moda_feminina. O schema
-- `public` (legado) jamais deve ser tocado por esta migration.
SET search_path TO baianas_moda_feminina;

BEGIN;

-- 1) Backup de TODAS as FKs que referenciam as 4 tabelas alvo (products,
--    suppliers, cart_items, order_items) — cobre id e product_id. Preserva
--    nome e definição originais para restauração fiel.
CREATE TEMP TABLE _fk_backup AS
SELECT
    con.conname                                                      AS conname,
    con.conrelid::regclass::text                                     AS tbl,
    pg_get_constraintdef(con.oid)                                    AS def
FROM pg_constraint con
WHERE con.contype = 'f'
  AND con.confrelid::regclass::text IN ('products', 'suppliers', 'cart_items', 'order_items');

-- 2) Remover temporariamente essas FKs e as PKs das 4 tabelas (o ALTER COLUMN
--    TYPE exige a ausência dessas constraints).
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT tbl, conname FROM _fk_backup LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', r.tbl, r.conname);
    END LOOP;

    FOR r IN
        SELECT con.conname, con.conrelid::regclass::text AS tbl
        FROM pg_constraint con
        WHERE con.contype = 'p'
          AND con.conrelid::regclass::text IN ('products', 'suppliers', 'cart_items', 'order_items')
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3) Converter colunas legadas VARCHAR -> UUID
ALTER TABLE products
    ALTER COLUMN id TYPE UUID USING id::uuid;

ALTER TABLE suppliers
    ALTER COLUMN id TYPE UUID USING id::uuid;

ALTER TABLE cart_items
    ALTER COLUMN id TYPE UUID USING id::uuid,
    ALTER COLUMN product_id TYPE UUID USING product_id::uuid;

ALTER TABLE order_items
    ALTER COLUMN id TYPE UUID USING id::uuid,
    ALTER COLUMN product_id TYPE UUID USING product_id::uuid;

-- 4) Recriar PKs das 4 tabelas
ALTER TABLE products    ADD PRIMARY KEY (id);
ALTER TABLE suppliers   ADD PRIMARY KEY (id);
ALTER TABLE cart_items  ADD PRIMARY KEY (id);
ALTER TABLE order_items ADD PRIMARY KEY (id);

-- 5) Restaurar TODAS as FKs referenciando as 4 tabelas (nomes/definições
--    originais). Se alguma não puder ser recriada (ex.: product_supplier com
--    colunas FK ainda VARCHAR), emite NOTICE e continua — o create_all() a
--    recriará quando (re)criar a tabela.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT tbl, conname, def FROM _fk_backup LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = r.tbl) THEN
            BEGIN
                EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', r.tbl, r.conname, r.def);
            EXCEPTION WHEN others THEN
                RAISE NOTICE 'FK % não restaurada em %I (create_all cuidará se aplicável): %',
                            r.conname, r.tbl, SQLERRM;
            END;
        END IF;
    END LOOP;
END $$;

-- 6) Verificação explícita: esperado data_type = uuid / udt_name = uuid
--    Restrita a baianas_moda_feminina (table_schema) para não confundir com public.
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_schema = 'baianas_moda_feminina'
  AND table_name IN ('products', 'suppliers', 'cart_items', 'order_items')
  AND column_name IN ('id', 'product_id')
ORDER BY table_name, column_name;

COMMIT;
