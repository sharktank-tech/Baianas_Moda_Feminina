# Opt-out da newsletter (P0 da auditoria T01)

Implementado em `feature/newsletter-opt-out` — PR aberto, sem merge/deploy.

## Comportamento

- **Link seguro**: token `{subscriber_id_hex}.{HMAC-SHA256(SECRET_KEY, "newsletter-unsubscribe:{id}")[:32]}` — sem e-mail na URL, sem login. Rota `GET/POST /newsletter/cancelar/<token>`.
- **GET** mostra apenas a confirmação (sem alterar estado); **cancelamento só por POST explícito** (`acao=cancelar`). POST idempotente.
- **Supressão**: `unsubscribed_at` preenchido = cancelado. Consultada em `subscribe_newsletter` (não reenvia boas-vindas) e exposta como `is_marketable(email)` / `is_subscription_suppressed(email)` para qualquer envio promocional futuro.
- **Cadastro repetido não reativa**: e-mail cancelado recebo mensagem orientando o link de reativação; linha intocada, zero e-mails.
- **Reinscrição explícita**: POST `acao=reativar` no mesmo token (página de confirmação oferece as duas ações). Registra `resubscribed_at`.
- **Transacionais separadas**: supressão só no módulo da newsletter; `checkout_recovery` (recuperação de pedido) e formulário de contato não consultam a supressão — testado por contrato em `test_fluxo_comunicacoes_transacionais_separadas`.

## Migração (0041)

- `migrations/0041_newsletter_unsubscribe.sql` — `ADD COLUMN IF NOT EXISTS` (aditivo, idempotente, schema `pollomodas`).
- `migrations/0041_newsletter_unsubscribe_down.sql` — reversão (perde histórico de cancelamento).
- `migrations/validate_0041.py` — confere tipo/nullable no Postgres (`DATABASE_URL`).
- O app também cria as colunas no boot (`loja/schema.py: REQUIRED_COLUMNS`); aplicar o SQL antes do deploy evita o primeiro erro.
- **Validação isolada**: suíte em SQLite (`tests/test_newsletter_unsubscribe.py`, 12 testes) cobre serviço + rotas; `validate_0041.py` roda contra Postgres descartável quando houver (`DATABASE_URL=... python migrations/validate_0041.py`).

## Ressalvas

- Assinantes antigos (antes desta branch) continuam ativos; o link de cancelamento entra no e-mail de boas-vindas. Para quem já era inscrito, o link pode ser gerado sob demanda (mesma função `generate_unsubscribe_token`).
- Rotação de `SECRET_KEY` invalida tokens emitidos (comportamento documentado; usuário pede novo link).
- Sem envio de e-mail em massa nesta rodada; sem campanhas recorrentes até revisão desta entrega.
