-- DOWN de 0027_password_reset.sql — reversível e idempotente.
-- Remove limitadores, tokens de recuperação e a coluna de versão de
-- autenticação (sessões/cookies vigentes voltam a ser aceitos sem versão).

DROP TABLE IF EXISTS baianas_moda_feminina.auth_rate_limits;
DROP TABLE IF EXISTS baianas_moda_feminina.password_reset_tokens;
ALTER TABLE baianas_moda_feminina.users DROP COLUMN IF EXISTS auth_version;
