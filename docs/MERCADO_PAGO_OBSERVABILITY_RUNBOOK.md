# Mercado Pago — Plano de Observabilidade e Operação Controlada

## Objetivo

Este documento registra a autorização operacional para conduzir a investigação da integração Mercado Pago Checkout Pro da PolloModas com foco em observabilidade, diagnóstico e segurança operacional.

O objetivo é identificar com evidência reproduzível em qual etapa o fluxo de pagamento é interrompido, sem alterar a lógica de cobrança até que a instrumentação seja validada e sem executar ações financeiras não autorizadas.

## Estado de segurança atual

Até nova autorização explícita, ficam congeladas novas tentativas reais de pagamento.

Guardrails obrigatórios:

```text
REAL_PAYMENT_RETRY=false
ALIEXPRESS_REAL_SUBMISSION=false
NO_PAID_RESOURCES=true
NO_SECRET_EXPOSURE=true
NO_PRODUCTION_DB_DESTRUCTIVE_CHANGES=true
NO_PAYMENT_REFUND_OR_CAPTURE=true
NO_CREDENTIAL_ROTATION=true
NO_PAYMENT_LOGIC_CHANGE=true
NO_PRODUCTION_ENV_SECRET_CHANGE=true
```

Também permanecem proibidos sem autorização específica:

- habilitar envio real de pedidos ao AliExpress;
- criar recursos pagos;
- capturar, reembolsar, cancelar ou movimentar dinheiro;
- executar DDL destrutivo em Production;
- alterar credenciais ou segredos;
- modificar a lógica de cobrança do Checkout Pro;
- realizar nova tentativa de pagamento real antes da conclusão da instrumentação e da validação descritas neste documento.

## Evidência já estabelecida

A investigação anterior confirmou:

- credencial Mercado Pago de produção válida;
- owner da credencial não-test e site `MLB`;
- criação de preference LIVE funcionando;
- `init_point` de produção retornado corretamente;
- collector da preference compatível com o owner da credencial;
- `external_reference` consistente entre preference e merchant order;
- valor da preference consistente com a merchant order;
- webhook da aplicação acessível e respondendo HTTP 200 para `topic_merchant_order_wh`;
- isolamento lógico de Preview validado via `POLLOMODAS_SCHEMA=pollomodas_preview`;
- escrita de teste em Preview provada sem alterar Production;
- Production sem Payment persistido e sem `mp_payment_id` nas orders analisadas;
- tentativa real recente criou `merchant_order` com `status=opened`, `order_status=payment_required`, `paid_amount=0`, `payments=[]`;
- busca por `external_reference` no Mercado Pago retornou zero Payments;
- não há evidência de erro de aplicação, banco ou webhook causando a ausência do Payment.

Conclusão operacional atual: o Checkout Pro chega à merchant order, mas o Mercado Pago não cria um objeto Payment nas tentativas observadas. Como não há Payment, não existe `status_detail` público para indicar a causa da interrupção.

## Plano autorizado

### 1. Congelar novas tentativas reais

Nenhuma nova tentativa LIVE será executada enquanto este runbook estiver em estado `REAL_PAYMENT_RETRY=false`.

### 2. Auditoria completa read-only de Production

Auditar, sem escrita:

- GitHub: branch `master`, commit efetivamente publicado, código do fluxo Mercado Pago, diferenças relevantes, testes e histórico recente;
- Vercel: deployment Production atual, branch/commit, runtime logs, build logs, status HTTP, callbacks e webhooks;
- Supabase: integridade das tabelas relevantes, orders/payments, vínculos `mp_*`, constraints/índices relevantes e ausência de divergência Preview/Production;
- Mercado Pago: somente endpoints GET públicos necessários ao diagnóstico, sem expor tokens ou IDs sensíveis em logs/documentação.

Toda evidência deve ser correlacionada por horário e identificadores sanitizados.

### 3. Branch dedicada de observabilidade

Branch operacional:

```text
ops/mercadopago-observability
```

A branch nasce do `master` validado e não deve tocar Production enquanto a instrumentação não passar no Preview.

### 4. Instrumentação estruturada e sanitizada

Adicionar logs de observabilidade ao fluxo Mercado Pago sem alterar a decisão de negócio ou o comportamento de cobrança.

Campos mínimos por evento, quando disponíveis:

```text
correlation_id
preference_id_hash
merchant_order_id_hash
payment_id_hash
external_reference_hash
webhook_type
x_request_id_hash ou forma sanitizada
provider_http_status
deployment_id
deployment_environment
git_branch
git_commit
order_id_hash
order_status_before
order_status_after
payment_status
payment_status_detail
flow_stage
event_name
```

Requisitos de privacidade:

- nunca registrar Access Token;
- nunca registrar webhook secret;
- nunca registrar CPF/documento;
- nunca registrar cartão, PAN, CVV ou dados bancários;
- nunca registrar e-mail completo, telefone completo ou endereço;
- nunca registrar IDs completos de buyer/payer quando não forem necessários;
- usar hash estável/curto apenas para correlação operacional;
- não registrar query strings sensíveis de OAuth ou callbacks;
- não registrar corpos de provider responses sem allowlist de campos.

### 5. Eventos mínimos a instrumentar

#### Criação da preference

Registrar início, sucesso e falha de:

```text
mercadopago.preference.create.start
mercadopago.preference.create.success
mercadopago.preference.create.failure
```

Em sucesso, registrar apenas metadados sanitizados e status HTTP do provider.

#### Redirecionamento ao Checkout Pro

Registrar:

```text
mercadopago.checkout.redirect
```

Sem registrar a URL completa se contiver token/parâmetros sensíveis.

#### Webhook

Registrar:

```text
mercadopago.webhook.received
mercadopago.webhook.signature_valid
mercadopago.webhook.signature_invalid
mercadopago.webhook.dispatch
mercadopago.webhook.completed
mercadopago.webhook.failure
```

Para `topic_merchant_order_wh`, registrar hash do merchant order e tipo do webhook; não assumir existência de Payment.

#### Sincronização de Payment

Registrar:

```text
mercadopago.payment.fetch.start
mercadopago.payment.fetch.success
mercadopago.payment.fetch.failure
mercadopago.order.transition
mercadopago.fulfillment.triggered
```

A transição deve registrar somente `before -> after`, nunca PII.

### 6. Correlation ID

Cada fluxo deve possuir um `correlation_id` próprio, preferencialmente propagado entre:

- criação da preference;
- order local;
- redirecionamento;
- retorno do checkout;
- webhook;
- sincronização do Payment.

Quando o provider não propagar o mesmo ID, correlacionar por `external_reference_hash`, `preference_id_hash`, `merchant_order_id_hash`, timestamp e deployment.

### 7. Validação obrigatória em Preview

A instrumentação deve ser validada exclusivamente no Preview configurado com:

```text
POLLOMODAS_SCHEMA=pollomodas_preview
```

Critérios de aprovação:

- Preview retorna HTTP 200;
- logs aparecem no Vercel;
- nenhum segredo ou PII aparece nos logs;
- nenhum registro é criado em `pollomodas` durante testes Preview;
- somente `pollomodas_preview` pode sofrer escrita nos testes controlados;
- nenhuma chamada de cobrança LIVE é executada;
- AliExpress permanece hard-off;
- nenhuma alteração de schema Production é necessária;
- testes existentes do fluxo Mercado Pago continuam passando ou, quando não executáveis no ambiente, a limitação é documentada explicitamente.

### 8. Promoção para Production

Somente após Preview aprovado, a observabilidade poderá ser proposta para Production.

A promoção para Production deve:

- não alterar lógica de cobrança;
- não alterar credenciais;
- não alterar valores;
- não alterar `init_point`;
- não habilitar novos providers;
- não habilitar AliExpress real;
- não alterar schema de Production;
- ser limitada a logs/telemetria e, se necessário, pequenos helpers puros de sanitização/correlação.

Antes do merge/deploy em Production, revisar diff e confirmar que não há mudança funcional no pagamento.

### 9. Próxima tentativa real controlada

Após observabilidade publicada e validada em Production, poderá ser considerada no máximo uma nova tentativa LIVE, somente se ainda necessária.

Antes da tentativa, confirmar:

```text
REAL_PAYMENT_RETRY=true
```

Essa mudança de estado exige autorização explícita do responsável.

Após a tentativa:

- não repetir automaticamente;
- capturar horário exato;
- correlacionar Vercel, Supabase e Mercado Pago;
- consultar merchant order e Payment somente por leitura;
- se houver Payment, registrar `status` e `status_detail` sanitizados;
- se não houver Payment, registrar a ausência como evidência para suporte do provider.

### 10. Critério de escalonamento ao Mercado Pago

Se, com observabilidade validada, ocorrer novamente:

```text
merchant_order.status=opened
merchant_order.order_status=payment_required
merchant_order.payments=[]
payment_search_count=0
```

então considerar o diagnóstico de aplicação encerrado para essa etapa e abrir chamado técnico no Mercado Pago com:

- horário BRT e UTC;
- merchant order ID fornecido somente ao suporte;
- hash/correlation ID interno;
- preference ID fornecido somente ao suporte quando necessário;
- HTTP status das APIs consultadas;
- sequência dos eventos Vercel;
- confirmação de ausência de Payment;
- confirmação de inexistência de erro de aplicação;
- confirmação de que nenhuma nova tentativa foi repetida após a coleta.

## Estado operacional

No momento da criação deste documento:

```text
INFRASTRUCTURE_INVESTIGATION_AUTHORIZED=true
OBSERVABILITY_BRANCH=ops/mercadopago-observability
PRODUCTION_AUDIT_MODE=READ_ONLY
REAL_PAYMENT_RETRY=false
PREVIEW_ISOLATION=PASS
ALIEXPRESS_REAL_SUBMISSION=false
NO_PAID_RESOURCES=true
NO_SECRET_EXPOSURE=true
NO_PRODUCTION_DB_DESTRUCTIVE_CHANGES=true
NO_PAYMENT_REFUND_OR_CAPTURE=true
NO_PAYMENT_LOGIC_CHANGE=true
```

## Regra de mudança de escopo

Qualquer ação fora deste runbook exige autorização explícita antes da execução, especialmente:

- nova cobrança real;
- refund/capture/cancel;
- mudança de segredo/credencial;
- recurso pago;
- alteração destrutiva de banco;
- mudança funcional no fluxo de pagamento;
- habilitação de submissão real ao AliExpress.

## Resultado esperado

Ao final, a infraestrutura deve permitir responder, com evidência correlacionada e sem exposição de dados sensíveis:

1. qual deployment processou o evento;
2. qual order local estava envolvida;
3. qual preference originou o checkout;
4. se houve merchant order;
5. se houve Payment;
6. qual status/status_detail o provider retornou, quando existente;
7. qual transição ocorreu na aplicação;
8. se houve falha de aplicação ou se a interrupção ocorreu exclusivamente dentro do Mercado Pago.
