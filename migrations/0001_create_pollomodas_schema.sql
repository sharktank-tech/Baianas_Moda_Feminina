-- Migration 0001: criação do schema dedicado da PolloModas (INSTALAÇÃO NOVA).
--
-- Este é o ponto de partida para um ambiente limpo. O schema `public` pertence
-- ao sistema legado e NÃO deve ser alterado pela PolloModas.
--
-- O app também executa `CREATE SCHEMA IF NOT EXISTS pollomodas` no boot
-- (defesa aditiva), mas este arquivo documenta e torna explícito o passo de
-- provisionamento do schema.
--
-- NÃO cria tabelas aqui: db.create_all() + ensure_schema() cuidam disso com
-- base nos models. Esta migration apenas garante o namespace.
--
-- Aplicação (uma vez, com backup prévio):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_create_pollomodas_schema.sql

CREATE SCHEMA IF NOT EXISTS pollomodas;
