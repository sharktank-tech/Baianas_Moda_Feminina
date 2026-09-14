-- Migration 0021: tornar o schema pollomodas explicitamente privado.
-- A aplicação Flask conecta diretamente ao PostgreSQL como papel de backend.
-- Nenhum acesso via PostgREST (anon/authenticated/service_role) é necessário.
\set ON_ERROR_STOP on

REVOKE ALL PRIVILEGES ON SCHEMA pollomodas
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA pollomodas
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA pollomodas
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA pollomodas
    FROM PUBLIC, anon, authenticated, service_role, authenticator;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA pollomodas
    REVOKE ALL PRIVILEGES ON TABLES
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA pollomodas
    REVOKE ALL PRIVILEGES ON SEQUENCES
    FROM PUBLIC, anon, authenticated, service_role, authenticator;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA pollomodas
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
        IF has_schema_privilege(role_name, 'pollomodas', 'USAGE') THEN
            RAISE EXCEPTION '% unexpectedly has USAGE on pollomodas', role_name;
        END IF;
    END LOOP;
END $$;
