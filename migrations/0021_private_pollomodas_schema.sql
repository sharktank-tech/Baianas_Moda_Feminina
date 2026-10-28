-- Migration 0021: tornar o schema baianas_moda_feminina explicitamente privado.
-- A aplicação Flask conecta diretamente ao PostgreSQL como papel de backend.
-- Nenhum acesso via PostgREST (anon/authenticated/service_role) é necessário.
\set ON_ERROR_STOP on

REVOKE ALL PRIVILEGES ON SCHEMA baianas_moda_feminina
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA baianas_moda_feminina
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA baianas_moda_feminina
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA baianas_moda_feminina
    FROM PUBLIC, anon, authenticated, service_role, authenticator;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA baianas_moda_feminina
    REVOKE ALL PRIVILEGES ON TABLES
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA baianas_moda_feminina
    REVOKE ALL PRIVILEGES ON SEQUENCES
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA baianas_moda_feminina
    REVOKE ALL PRIVILEGES ON FUNCTIONS
    FROM PUBLIC, anon, authenticated, service_role, authenticator;

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'anon', 'authenticated', 'service_role', 'authenticator'
    ]
    LOOP
        IF has_schema_privilege(role_name, 'baianas_moda_feminina', 'USAGE') THEN
            RAISE EXCEPTION '% unexpectedly has USAGE on baianas_moda_feminina', role_name;
        END IF;
    END LOOP;
END $$;
