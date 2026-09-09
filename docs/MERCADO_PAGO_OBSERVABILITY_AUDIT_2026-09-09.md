# Mercado Pago — Auditoria de Infraestrutura e Checkpoint de Observabilidade

Data: 2026-09-09

Branch de trabalho: `ops/mercadopago-observability`

Este documento complementa `MERCADO_PAGO_OBSERVABILITY_RUNBOOK.md` e registra as evidências coletadas e as alterações realizadas sob a autorização operacional vigente.

## Guardrails vigentes

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

## Auditoria read-only de Production

### GitHub

- `master` permanece no commit `20a52013a0cc8c5c2607593626e62a1cf817a604`.
- O fluxo de pagamento em Production continua usando `init_point` LIVE.
- O lock PostgreSQL está restrito a `Order` com `with_for_update(of=Order)`.
- Nenhuma alteração desta fase foi aplicada a `master`.

### Vercel Production

Deployment auditado:

```text
deployment=dpl_Ed3JB892k8pU2dMGgQkrTJNRMUjN
branch=master
commit=20a52013a0cc8c5c2607593626e62a1cf817a604
state=READY
region=iad1
```

Na tentativa LIVE mais recente foi observado:

```text
POST /pagamentos/webhook
webhook_type=topic_merchant_order_wh
http_status=200
```

Não foram encontrados logs `error` ou `fatal` do runtime de Production na janela auditada em torno da tentativa.

### Supabase Production

Baseline read-only observado:

```text
prod_orders=23
prod_payments=0
prod_orders_with_payment_id=0
prod_orders_with_preference=23
prod_pending_payment=23
prod_paid=0
```

O estado é consistente com o diagnóstico do provider: preferences/merchant orders existem, porém nenhum objeto Payment foi criado para as tentativas analisadas.

Advisors de performance apontaram índices/FKs não otimizados e advisors de segurança apontaram achados no schema legado `public`. Nenhum desses achados explica a ausência do objeto Payment no Mercado Pago, e nenhuma correção de banco foi feita nesta fase.

## Evidência do provider já correlacionada

A merchant order mais recente analisada apresentou:

```text
status=opened
order_status=payment_required
paid_amount=0
payment_count=0
payment_search_count=0
```

Portanto não existe `status_detail` público, pois nenhum Payment foi criado.

## Instrumentação implementada na branch

Foram adicionados helpers de observabilidade sem alterar a lógica de cobrança:

- `loja/observability/mercado_pago.py`
  - hash estável curto para identificadores;
  - correlação por `correlation_id`;
  - contexto Vercel de deployment/environment/branch/commit/region;
  - sanitização e redação de dados sensíveis;
  - logs JSON prefixados por `mp_observability`.
- `loja/observability/install.py`
  - eventos de entrada/saída nas rotas `/pagamentos/*`;
  - hash de `merchant_order`, Payment e external reference quando disponíveis;
  - captura sanitizada de `x-request-id`;
  - transições de `Order.status` via SQLAlchemy sem PII.
- `payment_service.py`
  - evento antes/depois de criar preference;
  - status HTTP do provider;
  - hash da preference/external reference/collector;
  - evento antes/depois de consultar Payment;
  - `status` e `status_detail` apenas quando o provider realmente retornar Payment.
- `tests/test_payment_observability.py`
  - teste de hash determinístico;
  - teste de redação de segredos/PII;
  - teste de ausência de preference/e-mail em texto puro no log.

Uma proteção adicional foi aplicada ao `correlation_id`: valores recebidos externamente só são aceitos se corresponderem ao conjunto seguro `[A-Za-z0-9._:-]` e ao limite de 64 caracteres; caso contrário um ID local seguro é gerado. Mensagens do provider são normalizadas e truncadas.

## Validação Vercel da instrumentação

O Preview da branch compilou e ficou `READY`.

Uma requisição GET, sem escrita e sem chamada ao Mercado Pago, produziu eventos estruturados como:

```text
http.payment_request.received
http.payment_request.completed
```

A validação confirmou no log:

- `correlation_id` presente;
- deployment ID presente;
- `environment=preview`;
- branch e commit presentes;
- external reference em hash;
- Payment ID em hash;
- query não copiada para o JSON estruturado;
- nenhum Access Token, webhook secret, e-mail, telefone, cartão ou endereço no evento estruturado.

## Isolamento do Preview — variável confirmada

Em 2026-09-09 foi confirmada no Vercel a variável branch-specific:

```text
POLLOMODAS_SCHEMA   Hidden   Secret   Preview (ops/mercadopago-observability)
```

A variável foi criada exclusivamente para a branch `ops/mercadopago-observability`. Como variáveis de ambiente Vercel só passam a valer em novos deployments, este commit documental existe também para disparar um novo Preview e permitir validar o isolamento no runtime efetivo.

A existência da variável, isoladamente, não encerra o gate. O novo deployment ainda deve provar:

- `READY`;
- HTTP 200;
- leitura do schema `pollomodas_preview`;
- ausência de alteração em `pollomodas`;
- logs estruturados sanitizados;
- nenhuma chamada LIVE ao Mercado Pago.

Até essa prova terminar, o estado permanece conservador:

```text
OBSERVABILITY_PREVIEW_SCHEMA_ENV=PASS
OBSERVABILITY_PREVIEW_RUNTIME_ISOLATION=PENDING_REDEPLOY_VALIDATION
OBSERVABILITY_PREVIEW_SAFE_FOR_WRITES=false
```

## Próximo gate

Validar o novo Preview gerado após a configuração do environment branch-specific. Primeiro serão executadas apenas leituras sem efeito colateral. Somente após confirmar que o runtime efetivo usa `pollomodas_preview` poderá ser considerada uma escrita sintética e não financeira em Preview.

## Estado do checkpoint

```text
PRODUCTION_AUDIT_READ_ONLY=PASS
PRODUCTION_UNCHANGED=true
OBSERVABILITY_BRANCH_CREATED=true
OBSERVABILITY_CODE_IMPLEMENTED=true
OBSERVABILITY_STRUCTURED_LOG_SMOKE=PASS
OBSERVABILITY_LOG_SANITIZATION_SMOKE=PASS
OBSERVABILITY_PREVIEW_BUILD=PASS
OBSERVABILITY_PREVIEW_SCHEMA_ENV=PASS
OBSERVABILITY_PREVIEW_RUNTIME_ISOLATION=PENDING_REDEPLOY_VALIDATION
OBSERVABILITY_PREVIEW_SAFE_FOR_WRITES=false
REAL_PAYMENT_RETRY=false
READY_FOR_PRODUCTION_MERGE=false
```
