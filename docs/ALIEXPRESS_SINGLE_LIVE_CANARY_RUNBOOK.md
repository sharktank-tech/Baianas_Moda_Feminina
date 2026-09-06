# FASE 8.0.11I — Single Live Canary Runbook

Status: **READINESS ONLY — NÃO AUTORIZA HTTP REAL**

Base congelada esperada: `679209f25fa727b34cf68bda474a6e280c814999`.

Este runbook define a preparação, os abort criteria e a observação de **um único** canário real de `aliexpress.trade.buy.placeorder`. Ele não altera gates, não seleciona pedido sozinho, não habilita provider e não autoriza merge/deploy/HTTP real.

## 1. Invariantes obrigatórios

Antes do canário real, todos estes itens devem ser verdadeiros:

- migration `0012` aplicada no schema `pollomodas`;
- `aliexpress_submission_attempts` e `aliexpress_remote_orders` existentes;
- zero attempts globais em `in_flight` ou `unknown`;
- zero pedidos em `pending_reconciliation`;
- código da FASE 8.0.11H em Production;
- `ALIEXPRESS_OAUTH_REFRESH_ENABLED=0`;
- `ALIEXPRESS_ORDER_STATUS_SYNC_ENABLED=0`;
- reconciliation padrão permanece hard-off;
- `ALIEXPRESS_CONTROLLED_CANARY_ENABLED` permanece OFF até a janela autorizada;
- `ALIEXPRESS_PLACEORDER_ENABLED` permanece OFF até a janela autorizada;
- nenhum retry automático, retry por idade ou release de stale attempt;
- apenas uma `AliExpressOrder` pode ser allow-listed;
- a ordem escolhida deve estar em `ready`, com supplier, credential válida e submission snapshot persistido/estruturalmente válido.

## 2. Critérios de seleção do único pedido

A seleção é **manual e explícita**. Não escolher automaticamente “primeiro”, “mais antigo” ou “mais barato”. O operador deve informar a `AliExpressOrder.id` exata após revisar o pedido.

O pedido candidato deve satisfazer:

1. `state == ready`;
2. `supplier_id IS NOT NULL`;
3. nenhum `AliExpressSubmissionAttempt` bloqueador para a ordem;
4. nenhum blocker global em qualquer outra ordem;
5. snapshot persistido presente e válido;
6. itens de fulfillment íntegros e sem estado `blocked`;
7. credencial do supplier válida no momento do preflight;
8. nenhum `AliExpressRemoteOrder` já persistido para a ordem;
9. `ali_order_id IS NULL`;
10. `attempt_count == 0` para o primeiro canário real, salvo autorização separada e análise explícita.

Se qualquer item falhar: **ABORTAR**. Não corrigir automaticamente o pedido durante a janela do canário.

## 3. Pre-arm — leitura apenas

Antes de ligar qualquer gate:

1. confirmar SHA de Production;
2. confirmar saúde do banco;
3. consultar apenas contagens sanitizadas:
   - attempts `in_flight`;
   - attempts `unknown`;
   - pedidos `pending_reconciliation`;
4. validar o pedido candidato por ID explícito;
5. confirmar que o provider real continua inacessível porque `ALIEXPRESS_PLACEORDER_ENABLED != 1`;
6. confirmar que o controlled canary continua inacessível porque `ALIEXPRESS_CONTROLLED_CANARY_ENABLED != 1`.

Nenhum POST para AliExpress ocorre nesta etapa.

## 4. Ordem de armamento — somente após autorização explícita separada

Quando houver autorização específica para executar o canário real:

1. definir `ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID=<UUID exato>`;
2. definir `ALIEXPRESS_CONTROLLED_CANARY_ENABLED=1`;
3. confirmar novamente que o circuit breaker global continua fechado;
4. definir `ALIEXPRESS_PLACEORDER_ENABLED=1` **por último**;
5. executar exatamente uma submissão pelo boundary `submit_controlled_canary_order(...)` com `get_real_order_submitter()` injetado explicitamente;
6. não repetir a chamada, independentemente do resultado;
7. desligar `ALIEXPRESS_PLACEORDER_ENABLED` imediatamente após a tentativa;
8. desligar `ALIEXPRESS_CONTROLLED_CANARY_ENABLED` imediatamente após a tentativa;
9. remover/desarmar `ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID` após coleta da evidência.

Nunca inverter a ordem para deixar o provider real habilitado antes do allow-list/canary gate.

## 5. Desfechos permitidos

### A. Confirmado

Esperado localmente:

- tentativa durável termina `confirmed`;
- parent termina `submitted`;
- exatamente uma coleção ordenada de `AliExpressRemoteOrder` é persistida;
- para cardinalidade 1, `ali_order_id` legado recebe o mesmo ID;
- nenhuma segunda chamada é permitida.

### B. Rejeitado conclusivo antes/por provider

Esperado:

- attempt termina `rejected`;
- parent permanece em estado de retry seguro do core (`submitting`) conforme contrato;
- **não fazer retry durante o canário**;
- gates devem ser desligados e o caso analisado fora da janela.

### C. Unknown / timeout / transport / protocolo ambíguo

Esperado:

- attempt termina `unknown`;
- parent termina `pending_reconciliation`;
- circuit breaker global fica ABERTO;
- desligar os gates imediatamente;
- **NUNCA reenviar**;
- nenhuma liberação por idade;
- exigir reconciliação remota conclusiva antes de qualquer nova submissão futura.

### D. Falha de persistência pós-provider

Tratar como ambígua mesmo se houver indício de sucesso remoto:

- parent `pending_reconciliation` best-effort;
- attempt `unknown` best-effort ou `in_flight` se a recuperação local também falhar;
- circuit breaker permanece ABERTO;
- **NUNCA reenviar**.

## 6. Abort criteria imediatos

Abortar antes do HTTP se ocorrer qualquer um:

- SHA de Production diferente do aprovado;
- migration 0012 ausente/incompleta;
- qualquer `in_flight`/`unknown` global;
- qualquer `pending_reconciliation`;
- target UUID não corresponde exatamente ao pedido revisado;
- order state diferente de `ready`;
- snapshot ausente/inválido;
- supplier/credential não elegível;
- remote order já persistida;
- `ali_order_id` já preenchido;
- `attempt_count != 0` sem autorização específica;
- OAuth refresh ou status sync ligados;
- reconciliation não hard-off;
- qualquer alteração inesperada de schema/config/deployment durante a janela.

Depois que o provider foi chamado, **não existe retry automático como ação de abort**. O fluxo passa a depender exclusivamente do desfecho persistido/ambíguo.

## 7. Evidência mínima pós-canário

Relatar apenas dados sanitizados:

- SHA Production;
- `AliExpressOrder.id` pode ser mantido apenas em canal operacional privado autorizado; não publicar em logs públicos;
- estado local final;
- `attempt.state` final;
- cardinalidade de remote IDs (0/1/N), sem listar IDs no relatório geral;
- quantidade de chamadas lógicas ao provider: deve ser 1;
- quantidade de rows `AliExpressRemoteOrder` para a ordem;
- circuit breaker final: OPEN/CLOSED;
- gates finais: todos OFF;
- erros sanitizados somente por código estável.

Nunca incluir token, credential, endereço, payload TOP, assinatura, `attempt_key` ou remote order IDs em relatório geral.

## 8. Critério de sucesso da FASE 8.0.11I

A fase só pode ser marcada `PASS` após um canário explicitamente autorizado e observado com:

- exatamente 1 chamada lógica ao provider;
- desfecho local consistente com o contrato;
- gates desligados novamente;
- nenhuma segunda submissão;
- nenhuma evidência de duplicidade.

Até existir autorização explícita para a tentativa real, o status correto é:

`FASE 8.0.11I — RUNBOOK READY / LIVE CANARY NOT AUTHORIZED`.
