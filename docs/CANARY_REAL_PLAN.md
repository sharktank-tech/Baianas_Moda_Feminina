# Plano Canário Real — AliExpress PlaceOrder (Somente Leitura até Autorização)

> **Status:** Planejamento. Gates AliExpress **DESLIGADOS** (`ALIEXPRESS_PLACEORDER_ENABLED != 1`, `ALIEXPRESS_CONTROLLED_CANARY_ENABLED != 1`, `ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED != 1`). Nenhuma submissão real será feita nesta etapa. Qualquer gasto com fornecedor exige autorização financeira explícita em etapa posterior.

## 1. Objetivo

Validar end-to-end, com um único pedido orgânico novo, que o fluxo `ready → snapshot → submission → remote identity` funciona sob as proteções de concorrência e economia já provadas em testes descartáveis (Fase 8.0.11I), **sem** efetuar compra real até autorização.

## 2. Pré-condições (todas devem estar verdes)

- **Pagamento canônico aprovado:** `payments` latest-first (`created_at DESC, id DESC`) com `status='approved'`, `amount == orders.total`, `currency='BRL'`, `orders.status == 'paid'` e `provider_payment_id` presente. Verificado por `scripts/verify_future_canary_readonly.py`.
- **Valor e moeda:** `orders.total` em BRL, `supplier_products.cost`/`shipping_cost` em mesma moeda, `freight_currency == currency` (`BRL`), `Decimal` quantizado `0.01`.
- **Dados de entrega:** `orders` com `zip_code, street, number, city, state, customer_name, customer_phone` presentes; telefone normalizado para `DD9XXXXXXXX` (11 dígitos, móvel BR) sem expor valor.
- **Snapshot íntegro:** `aliexpress_submission_snapshots` 1:1 com `aliexpress_orders`, `shipping_address` com `contact_person, full_name, mobile_no, phone_country +55, zip, address, city, province, country BR` e `items` com `order_item_id, product_id, sku_attr, product_count`; **correspondência completa** entre `order_items`, `aliexpress_order_items` e `snapshot.items` (mesma quantidade, `order_item_id` sem duplicado, sem item ausente).
- **Economia válida:** `products.pricing_status='priced'`, `target_margin_percent` em `(0,100)`, `supplier_products.freight_status='available'`, `landed_cost == cost + shipping_cost` quantizado, `price == landed / (1 - margin/100)` arredondado para cima; validado para **todos** os itens com `Decimal`.
- **Ausência de tentativas/remota:** `aliexpress_submission_attempts` e `aliexpress_remote_orders` zerados para o `ali_order_id` candidato; `ali_order_id IS NULL`.
- **Exclusão legado:** `aliexpress_orders.attempt_count != 5` (pedido legado com 5 tentativas excluído explicitamente).
- **Origem de aquisição registrada:** `orders.acquisition_source` não vazio e sem PII (ex.: `instagram`, `google`, `tiktok`, `facebook` via `capture_acquisition`); **não imprime valor bruto**, apenas `presente`. Valores `cpc`, `direct` ou domínio desconhecido (`dominio-desconhecido-xyz123.com`) são aquisição válida mas **não comprovam** cliente real.
- **Pedido real de cliente (prova independente pendente):** `acquisition_source` identifica apenas atribuição de tráfego; CPF com dígitos verificadores válidos, e-mail preenchido e `Payment` aprovado também podem existir em testes. O modelo `User` atual não possui estado de e-mail verificado. Antes de classificar um candidato como orgânico, um responsável deve conferir, em fontes independentes, (a) no painel oficial Mercado Pago, um pagamento de produção associado ao mesmo pedido, valor e moeda (sem credencial de teste); e (b) confirmação do cliente de que iniciou a compra, obtida por canal de atendimento autorizado. Registrar em trilha restrita apenas data, responsável, referência opaca das duas evidências e decisão, sem CPF, e-mail, telefone, payload ou ID integral no repositório ou logs. Se uma das duas evidências não estiver disponível ou houver divergência, manter `origem orgânica pendente`. A verificação read-only e a confirmação humana não autorizam compra no fornecedor.
- **Credencial:** `supplier` AliExpress com `seller_id` e credencial criptografada válida (`ALIEXPRESS_CREDENTIAL_KEY` configurada, `persist_credential` ok).
- **Gates desligados:** `ALIEXPRESS_PLACEORDER_ENABLED !=1` confirmado via `os.environ` e `loja` config.
- **Observabilidade pronta:** logs sem PII (sem `exc`/SQL params, sem `acquisition_source` bruto, sem `CPF`/`endereço`/`telefone`/`token`), métricas de snapshot, e `verify_future_canary_readonly.py` passando.

O procedimento de responsáveis, dupla evidência, registro restrito e decisão está em [CANARY_CUSTOMER_EVIDENCE_PROTOCOL.md](CANARY_CUSTOMER_EVIDENCE_PROTOCOL.md). Sem local privado aprovado para a trilha e sem evidências de um pedido específico, a confirmação permanece pendente. Nenhum contato com cliente é iniciado por este plano.

### Situação da verificação automática

`scripts/verify_future_canary_readonly.py` verifica as condições técnicas em transação somente leitura e retorna `1` mesmo quando os checks preliminares passam: a confirmação independente acima permanece fora do banco e pendente. Não use a existência de `acquisition_source`, CPF, conta ou pagamento como substituto das duas evidências. Antes de qualquer canário, repetir a leitura, conferir a trilha restrita e obter a autorização financeira separada.

## 3. Limites

- **Um único pedido:** canário controlado para exatamente um `AliExpressOrder` (`state='ready'`, `attempt_count=0`, sem `ali_order_id`), com `Barrier(2)` provando serialização se houver concorrência.
- **Sem HTTP real:** submissão injetada com `FakeSubmitter`/`SlowCountingSubmitter` (zero rede) até autorização; `HardOffOrderStatusSyncer` por padrão.
- **Valor teto:** `total <= R$ 50,00` e `cost + shipping <= R$ 30,00` para limitar exposição; se candidato exceder, aborta e aguarda novo pedido orgânico.
- **Janela:** execução somente em horário comercial com responsável financeiro e técnico presentes.
- **Tentativas:** máximo 1 tentativa real após autorização; `attempt_count` nunca deve ser manualmente zerado.

## 4. Observabilidade (sem PII)

- **Logs sanitizados:** `seller_id` mascarado, `mobile_no` como `presente/ausente`, `zip`/`city` como flags, `sku_attr` como `presente`, nunca `contact_person`, `address`, `token`, `code/state`.
- **Métricas:** `aliexpress_orders` (`state, attempt_count, queued_at, ready_at`), `aliexpress_submission_snapshots` (`created_at`), `aliexpress_submission_attempts` (`state, started_at`), `aliexpress_remote_orders` (`remote_order_id` mascarado).
- **Verificação:** `DATABASE_URL` com `SET TRANSACTION READ ONLY`; `CANARY_READONLY=1` obrigatório para hosts não descartáveis; `CANARY_ORDER_ID` opcional para alvo específico.
- **Rastreamento:** `request_id` do provider (quando houver) logado como `request_id=...` sem payload.

## 5. Critérios de Interrupção (fail-closed, sem compra)

- Pagamento não aprovado ou valor/moeda divergente.
- Dados de entrega incompletos ou telefone inválido.
- Snapshot ausente/incompleto ou `address` sem `province/city/country`.
- Economia inválida (`pricing_status != priced`, margem fora `(0,100)`, `freight_status != available`, `landed_cost` divergente).
- Tentativas/remota já existentes (`attempt_count >0`, `ali_order_id` presente, `attempts>0`).
- Pedido é o legado com `attempt_count ==5` (excluído).
- Credencial ausente ou `seller_id` divergente.
- Gate `ALIEXPRESS_PLACEORDER_ENABLED` habilitado sem autorização (deve estar `!=1`).
- Erro de infraestrutura (`lock_timeout`, `serialization_failure`) — aborta e re-verifica.
- Qualquer log com PII detectado — aborta e revisa sanitização.

Em qualquer interrupção, **não** habilitar gates, **não** submeter ao AliExpress, **não** efetuar compra; registrar motivo sanitizado e aguardar correção.

## 6. Próximos Passos (exigem autorização explícita)

1. Autorização financeira por escrito para gasto máximo (ex.: R$ 50,00) com fornecedor.
2. Habilitação temporária controlada: `ALIEXPRESS_PLACEORDER_ENABLED=1`, `ALIEXPRESS_CONTROLLED_CANARY_ENABLED=1`, `ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID=<uuid>` e `ALIEXPRESS_CREDENTIAL_KEY` em ambiente isolado.
3. Execução do canário real com `submit_controlled_canary_order` sob `SELECT ... FOR UPDATE` e `Barrier(2)` (prova de serialização), com `SlowCountingSubmitter` substituído por `AliExpressDSOrderSubmitter` real somente após autorização.
4. Confirmação de `AliExpressSubmissionAttempt` (`state=confirmed/in_flight`) e `AliExpressRemoteOrder` (`remote_order_id` presente, 1:1). **Atenção:** `ROLLBACK` local **não desfaz** compra já efetivada no AliExpress. Em divergência (`attempt_count` inesperado, `remote_order_id` ausente/duplicado ou `state` não `submitted`), **interromper imediatamente**, preservar evidências (snapshot, `attempts`, `remotes`, `ali_order_id`, logs sanitizados, `request_id`) e **reconciliar a operação remota** via `loja/integrations/aliexpress/reconciliation` e `status_sync` (consulta `aliexpress.trade.ds.order.get` somente com `ALIEXPRESS_ORDER_STATUS_SYNC_ENABLED=1`), sem nova tentativa automática. Compra e habilitação dos gates permanecem condicionadas a autorização financeira explícita.

## 7. Pendências Atuais

- `test_aliexpress_oauth` 12 falhas de ordem estabilizadas com autouse que limpa `ALIEXPRESS_*`/`VERCEL_*` (ver `tests/test_aliexpress_oauth.py:50-70`).
- `test_concurrent_shipped_vs_delivered_milestone_staging` relaxado para monotônica (`set(results) <= {shipped,delivered}`) — estado final `delivered` é determinístico.
- Verificação somente leitura pronta em `scripts/verify_future_canary_readonly.py` (testado em PG descartável `pollo_test_*`).

## 8. Referências

- `tests/test_aliexpress_controlled_canary_pg.py` (prova concorrência canário controlado)
- `tests/test_aliexpress_single_live_canary_pg.py` (one-shot)
- `loja/integrations/aliexpress/status_sync.py` (máquina monotônica)
- `loja/integrations/aliexpress/submission_snapshot.py` (snapshot 1:1)
