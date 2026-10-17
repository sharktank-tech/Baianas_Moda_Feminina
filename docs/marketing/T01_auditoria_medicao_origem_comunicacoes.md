# T01 — Auditoria de medição, origem e comunicações

Versão 1.0 · 16/10/2026 · SHA auditado: `f4dd706202649fcf568257a7f939b174c951a21e` (master).
Escopo: somente leitura do código; nenhum disparo, pagamento, campanha ou alteração de produção.

## 1. Convenção de afirmações (código × configuração × produção)

| Camada | O que foi pesquisado | Resultado |
|---|---|---|
| Código pesquisado | `fbq`, `gtag`, `ttq`, Meta/GA/TikTok em `loja/templates/`, `public/static/scripts/`; Conversions API / Measurement Protocol (`graph.facebook`, `google-analytics`, `business-api.tiktok`) em `loja/` | **Nenhuma ocorrência.** Nenhum evento de navegador (`Purchase`, `ViewContent`, etc.) e nenhuma integração servidor-para-plataforma no código |
| Configuração externa | Painéis de anúncio, pixels configurados fora do repo, chaves de API de marketing, webhooks cadastrados no Mercado Pago | **Não inspecionável pelo código.** Exige conferência manual (Fase 0 do plano) — nenhuma afirmação feita aqui |
| Produção | Tráfego real, entregas de e-mail, comportamento do gateway | **Não observado neste T01.** Afirmações abaixo valem para o código no SHA auditado |

Corolário: declara-se ausência **no código pesquisado**, não ausência de integração no negócio.

## 2. Medição — evidências

- Webhook Mercado Pago: `POST /pagamentos/webhook` (`loja/views/payments_routes.py:1256`) com `classify_webhook` + `validate_webhook_signature`.
- Página de retorno (`/pagamentos/retorno/<result>`, `payments_routes.py:1357`): não confia na URL; sincroniza pela API (`_sync_payment`) e usa `payment_is_confirmed`.
- Helper canônico de confirmação: `select_order_payment()` (`loja/services/payment_scenario.py:72`) — só `mercado_pago` + `approved` + `amount == total`, mais recente vence; sem candidato = evidência ausente.

## 3. Quem escreve no ledger e confirma pagamento

| Escritor | Onde | O que grava |
|---|---|---|
| Criação do pedido | `payments_routes.py:969` (+ itens) | `Order` com `status="pending_payment"` + snapshot `acquisition_*` (origem congelada pré-gateway) |
| Webhook assinado | `payments_routes.py:1151` (`Payment` upsert por `provider_payment_id`) + mapeamento `mp_status_to_order_status` | `Payment.status/detail/amount`, `Order.mp_*`, `Order.status`; `approved` tardio **nunca** reverte `refunded`/`charged_back`; divergência valor/moeda → `manual_review` (com `paid_at` se approved) |
| Retorno do gateway | `_sync_payment` (mesmo upsert canônico) | Mesmos campos, mesma precedência do ledger do webhook |
| Fluxos manuais | decisão `manual_review`, `send_single_checkout_recovery` | Transições auditadas; sem reescrita de `acquisition_*` em nenhum caminho pós-criação |

**Concorrência e idempotência: PENDENTES até teste (T04).** Webhook repetido, corrida webhook×retorno, pagamento pendente, valor divergente e reembolso exigem teste dedicado. **O risco não é classificado como baixo** — apenas documentado o desenho fail-closed atual.

## 4. Origem até o pedido (persistida, não reconstruída)

- Captura: `before_request` (`loja/views/routes.py:91`) → `capture_acquisition` (first-touch em `session["acquisition"]`, nunca reescrita).
- Persistência: `acquisition_to_order_kwargs(order_acquisition(session))` na criação do `Order` (`payments_routes.py:969`). Colunas `acquisition_source/medium/campaign/referrer` (`payment_models.py:186`).
- Gateway não vira origem comercial: nenhuma escrita em `acquisition_*` fora da criação (pesquisado em `payments_routes.py`); snapshot é dict congelado pré-redirect.
- Cobertura: `tests/test_acquisition_origin_flow.py` — primeira origem, navegação interna, segunda campanha, acesso direto, referrer externo, passagem pelo gateway.

## 5. Comunicações — o que é enviado hoje

| Envio | Gatilho | Deduplicação |
|---|---|---|
| Boas-vindas newsletter (cupom `PRIMEIRACOMPRA`) | Cadastro via `subscribe_newsletter()` | E-mail existente → sem reenvio; `IntegrityError` tratado |
| Contato → e-mail interno (`Config.EMAIL_DESTINE`) | Formulário de contato | Iniciado pelo usuário; não é marketing |
| Recuperação de checkout (individual e lote) | Rotas admin manuais (`/admin/recuperacao-checkout/...`) | Revalidação sob lock; lote com flag (desligado por padrão) |

Nenhum agendador (cron/APScheduler/Celery) encontrado; nenhum envio automático ativo no código. WhatsApp: zero ocorrências.

## 6. Pendência prioritária P0 — saída da newsletter

> **Status 17/10/2026:** implementado em `feature/newsletter-opt-out` (PR aberto;
> ver `docs/marketing/NEWSLETTER_UNSUBSCRIBE.md`). A trava de campanhas
> recorrentes permanece até merge/deploy e revisão da entrega.

- **Estado:** cadastro + boas-vindas existem; **nenhum fluxo de cancelamento (opt-out) encontrado no código**; sem lista de supressão; reinscrição do mesmo e-mail retorna mensagem amigável sem reenviar (não reinscreve, mas também não oferece saída).
- **Proposta (não implementada):** link de cancelamento simples (token por e-mail, sem login), tabela/coluna de supressão consultada antes de qualquer envio promocional, boas-vindas sem reenvio após saída.
- **Trava:** nenhuma campanha promocional recorrente antes de resolver isso (A7).

## 7. T04/T05 delimitadas (próximas, sem implementação)

- **T04** — Repetição e concorrência do webhook (mesmo `provider_payment_id` 2×; webhook×retorno simultâneos), pagamento pendente, valor/moeda divergente, reembolso pós-approved. Critério: uma compra lógica por pedido em todos os cenários; `refunded` nunca revertido.
- **T05** — Definir antes do endpoint: receita (pedidos + frete − descontos), pagamentos confirmados (helper canônico), reembolsos (ajuste, nunca receita), fuso (America/Araguaina?). Só então endpoint agregado restrito reaproveitando `channel_metrics.py`.

## 8. Limitações desta auditoria

- Vale para o SHA auditado; mudanças posteriores exigem reauditoria dos trechos.
- Configuração externa (pixels, CAPI, webhooks cadastrados, chaves) e produção não foram observadas.
- Concorrência/idempotência documentadas por leitura, sem teste de carga ou corrida.
