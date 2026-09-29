-- Fluxo "Esqueci minha senha" (PolloModas).
-- 1) auth_version: versão de autenticação por usuário; embutida no payload de
--    sessão/lembrar-me (User.get_id) e validada no user_loader — trocar a
--    senha invalida sessões e cookies "manter conectado" antigos.
-- 2) password_reset_tokens: guarda APENAS o hash sha256 do token (nunca o
--    token em texto puro); 1 token ativo por usuário (índice único parcial).
-- 3) auth_rate_limits: limite de tentativas por IP e por e-mail (janela fixa).
-- Idempotente. Seção final DOWN é reversível.

ALTER TABLE pollomodas.users
    ADD COLUMN IF NOT EXISTS auth_version integer NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS pollomodas.password_reset_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL
        REFERENCES pollomodas.users(id) ON DELETE CASCADE,
    token_hash varchar(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone,
    CONSTRAINT uq_password_reset_tokens_hash UNIQUE (token_hash),
    CONSTRAINT ck_password_reset_tokens_window
        CHECK (expires_at > created_at)
);

-- No máximo 1 token não consumido por usuário (nova solicitação revoga os
-- anteriores no serviço; o índice garante a unicidade mesmo sob corrida).
CREATE UNIQUE INDEX IF NOT EXISTS uq_password_reset_active_user
    ON pollomodas.password_reset_tokens (user_id)
    WHERE used_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user
    ON pollomodas.password_reset_tokens (user_id);

CREATE TABLE IF NOT EXISTS pollomodas.auth_rate_limits (
    scope varchar(32) NOT NULL,
    key_hash varchar(64) NOT NULL,
    window_started_at timestamp without time zone NOT NULL,
    attempts integer NOT NULL,
    PRIMARY KEY (scope, key_hash),
    CONSTRAINT ck_auth_rate_limits_attempts CHECK (attempts >= 0)
);

-- ──────────────────────────────────────────────────────────────────────────
-- DOWN reversível: ver 0027_password_reset_down.sql (testado em PostgreSQL
-- em tests/test_password_reset_pg.py).
-- ──────────────────────────────────────────────────────────────────────────
