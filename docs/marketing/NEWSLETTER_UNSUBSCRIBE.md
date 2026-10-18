# Opt-out da newsletter (P0 da auditoria T01)

Implementado em `feature/newsletter-opt-out` — PR aberto, sem merge/deploy.

## Comportamento

- **Link seguro**: token `{subscriber_id_hex}.{HMAC-SHA256(SECRET_KEY, "newsletter-unsubscribe:{id}")[:32]}` — sem e-mail na URL, sem login. Rota `GET/POST /newsletter/cancelar/<token>`.
- **GET** mostra apenas a confirmação (sem alterar estado); **cancelamento só por POST explícito** (`acao=cancelar`). POST idempotente.
- **Supressão**: `unsubscribed_at` preenchido = cancelado. Consultada em `subscribe_newsletter` (não reenvia boas-vindas) e exposta como `is_marketable(email)` / `is_subscription_suppressed(email)` para qualquer envio promocional futuro.
- **Recarga antes do envio**: `_send_welcome_email` recarrega o estado persistido (`populate_existing()` — sem reutilizar o estado da sessão) imediatamente antes da chamada ao provedor; objeto stale/detached não é confiado e falha na verificação impede o envio (fail-closed). **Janela residual**: mesmo com a recarga imediatamente anterior, existe uma janela entre a última verificação e a aceitação do envio pelo provedor — a recarga minimiza, mas não elimina, a corrida; cancelamentos nessa janela são reaplicados na próxima consulta (o link de cancelamento segue válido e o POST é idempotente).
- **Cadastro repetido não reativa**: e-mail cancelado recebe mensagem orientando o link de reativação; linha intocada, zero e-mails.
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

## Comunicações: classificação finalidade vs. técnica

- **Classificação por finalidade/conteúdo**: uma mensagem é transacional se seu propósito principal for comunicar informações ligadas a uma transação ou interação específica do usuário (ex.: pedido concluído, senha redefinida, alteração de cadastro). Uma mensagem é promocional se seu propósito é engajar, vender, divulgar novidades ou ofertas.
- O fato de o `checkout_recovery` não importar da newsletter **não o classifica automaticamente como transacional**: comprova apenas **isolamento técnico** (independência de imports). A classificação da comunicação continua dependendo da finalidade e do conteúdo da mensagem, e não da arquitetura de imports; uma mensagem de recuperação de checkout com apelo promocional deve ser tratada como promocional. A classificação como transacional exige ACID guarantees, idempotência garantida e entrega exactly-once — propriedades que dependem da implementação do provedor de e-mail e da arquitetura de envio, não apenas da ausência de imports da newsletter.
- **Documentação de elegibilidade**: todo envio deve consultar `is_subscription_suppressed(email)` antes de prosseguir, independentemente de ser classificado como transacional ou promocional. A regra de elegibilidade do `checkout_recovery` baseia-se em: `status == 'pending_payment'`, `stock_reserved == True`, `recovery_email_sent_at == None`, janela de idade configurada, e `customer_email` preenchido.

## Reversão

- **Executar o down (0041_newsletter_unsubscribe_down.sql) elimina o histórico de supressão**: remove `unsubscribed_at` e `resubscribed_at`. Inscritos anteriormente cancelados tornam-se elegíveis novamente automaticamente.
- **Rollback preferido (código)**: reverter o código preservando as colunas. As colunas são aditivas e idempotentes — podem permanecer em produção após o revert.
- **Risco do rollback de código sem salvaguarda**: se o código revertido voltar a enviar promoções sem consultar a supressão, cancelados voltará a receber e-mails indesejados — com as colunas presentes no banco, porém ignoradas pelo código antigo. **Antes de qualquer rollback de código, bloquear/disabilitar os disparos promocionais** (ex.: desativar envios em lote e automações) até que o código com a verificação de supressão esteja no ar. Somente após o código protegido em produção o rollback está completo.
- **Não apresentar down como reversão operacional segura**: perda permanente do histórico de cancelamento; apenas remover colunas via migração futura e planejada.
