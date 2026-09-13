# PolloModas — Roadmap de monetização

Checkpoint criado em 2026-09-12 para consulta futura.

## Estado atual

A loja já provou capacidade de monetização: existe pagamento Mercado Pago aprovado em modo real, catálogo AliExpress publicado e deploy de produção ativo. O objetivo das próximas fases não é apenas "conseguir cobrar", mas tornar o ciclo de venda repetível, lucrativo e operacionalmente seguro.

O fluxo-alvo é:

`produto publicado -> checkout -> pagamento aprovado -> fulfillment fornecedor -> rastreio -> entrega -> margem líquida positiva`

## FASE 8.0.12C — CPF nativo no checkout

**Status:** concluída e implantada.

Objetivo: capturar, validar e persistir o CPF do destinatário antes da criação da preferência do Mercado Pago, para que novos pedidos AliExpress já nasçam com identidade fiscal suficiente para materializar o submission snapshot.

Entregue:

- CPF informado no checkout, sem valor hardcoded;
- normalização para 11 dígitos e validação dos dígitos verificadores;
- nenhum CPF em logs, exceptions ou mensagens sanitizadas;
- `orders.customer_cpf` persistido para pedidos novos;
- pedidos legados continuam válidos com `customer_cpf = NULL`;
- snapshot novo recebe `cpf` diretamente da fonte persistida;
- falha de CPF acontece antes da chamada ao Mercado Pago;
- migration 0014 aplicada e validada no Supabase de produção;
- nenhuma chamada real AliExpress foi habilitada por esta fase.

## FASE 8.1.0 — Fulfillment AliExpress de produção

**Status:** implementação técnica concluída; ativação operacional pendente.

Objetivo: substituir o caminho operacional HARD-OFF/canário por um pipeline de produção explicitamente controlado para pedidos pagos.

Entregue:

- dupla trava para seleção do provider real;
- preflight read-only de pedido pago, integridade, snapshot e ledger;
- orquestrador one-shot sem retry automático;
- terceiro gate independente para auto-dispatch pós-pagamento;
- circuit breaker global antes da seleção do provider;
- `unknown` / `pending_reconciliation` bloqueiam novas submissões automáticas;
- observabilidade admin somente leitura para gates, breaker e estados do fulfillment;
- nenhuma tela administrativa habilita gates ou provider;
- nenhuma autorização de merge implica chamada ao provider.

Ativação operacional permanece fora do escopo automático: ligar gates em produção e qualquer nova chamada real ao AliExpress exigem autorização explícita própria.

## FASE 8.2.0 — Margem líquida e proteção contra venda com prejuízo

**Status:** concluída — checkpoints A–J entregues.

Objetivo: sair da margem baseada apenas em landed cost e calcular unit economics reais.

### Checkpoint 8.2.0-A — semântica canônica de unit economics ✅

Entregue:

- avaliador puro/read-only;
- custos classificados explicitamente como `known`, `not_applicable` ou `unknown`;
- `unknown` nunca é convertido silenciosamente para zero;
- margem de contribuição e margem operacional só são definitivas quando todos os componentes relevantes são conhecidos ou explicitamente não aplicáveis.

### Checkpoint 8.2.0-B — política explícita de custos ✅

Entregue:

- classificação centralizada dos componentes financeiros;
- validação fail-closed de valores ausentes ou inválidos;
- composição sem defaults financeiros implícitos.

### Checkpoint 8.2.0-C — adaptação de pedidos ✅

Entregue:

- receita bruta baseada em subtotal + frete cobrado;
- reconciliação do total cobrado com descontos;
- landed cost canônico obrigatório para resultado definitivo;
- nenhuma leitura retroativa do catálogo mutável.

### Checkpoint 8.2.0-D — snapshot financeiro no checkout ✅

Entregue:

- custo unitário, frete do fornecedor, landed cost e moeda congelados no `OrderItem`;
- snapshot somente quando existe exatamente um fornecedor ativo e os valores são coerentes;
- pedidos legados e custos incompletos permanecem `NULL`, nunca zero;
- migration aditiva e idempotente no schema `pollomodas`;
- nenhuma aplicação automática da migration em produção.

### Checkpoint 8.2.0-E — consumo automático do snapshot no pedido ✅

Entregue:

- landed cost total derivado exclusivamente dos snapshots dos itens;
- quantidade de cada item considerada na agregação;
- pedidos legados/incompletos permanecem inconclusivos;
- snapshots presentes porém incoerentes falham como erro de integridade;
- nenhuma consulta ao catálogo, fornecedor ou provider.

### Checkpoint 8.2.0-F — recebimento líquido do processador ✅

Entregue:

- valor cobrado e líquido recebido extraídos do payload persistido;
- retenção total calculada sem consulta ao Mercado Pago;
- ausência do fato permanece `unknown`;
- divergências de valor, moeda ou retenção falham fechado;
- nenhuma decomposição presumida entre tarifa e parcelamento.

### Checkpoint 8.2.0-G — retenção real no avaliador ✅

Entregue:

- retenção observada tem precedência sobre estimativas percentuais;
- gateway e parcelamento estimados nunca são somados à retenção real;
- ausência do fato usa a política configurada como fallback;
- vínculo pagamento/pedido e valor cobrado são validados;
- nenhuma leitura de provider ou escrita em produção.

### Checkpoint 8.2.0-H — avaliação líquida pré-publicação ✅

Integrado na master em 2026-09-13 via PR #42 (merge `80c924a`), sem migration e sem alteração em produção.

Entregue:

- simulação explícita da venda de uma unidade;
- gateway, parcelamento, imposto, CAC e reserva incluídos pela política;
- custo desconhecido bloqueia a decisão;
- margem operacional comparada a um mínimo obrigatório;
- avaliador puro, ainda sem ativar o bloqueio na rota administrativa.

### Checkpoint 8.2.0-I — guard líquido de publicação ✅

Integrado na master em 2026-09-13 via branch `feat/net-margin-publication-guard-8.2.0`, sem migration e sem alteração em produção.

Entregue:

- avaliador líquido conectado ao caminho canônico de publicação;
- gate `UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED` default-off;
- somente o valor literal `1` habilita o bloqueio;
- margem mínima e política incompletas bloqueiam quando habilitado;
- ativação em produção continua fora do escopo automático.

Componentes ainda previstos para integração posterior:

- taxa do gateway;
- custo de parcelamento quando aplicável;
- impostos configurados pelo negócio;
- custo de aquisição por pedido/canal para análise gerencial;
- reserva operacional para reembolso/chargeback, se adotada.

Critérios de saída da fase completa:

- produto não é publicado abaixo da margem líquida mínima definida;
- nenhum custo desconhecido é silenciosamente tratado como zero;
- dashboard mostra receita bruta, custo, margem de contribuição e margem líquida operacional definida pelo projeto.

### Checkpoint 8.2.0-J — dashboard administrativo read-only ✅

Integrado na master via rota `GET /admin/unit-economics` (somente leitura), sem migration e sem alteração em produção.

Entregue:

- agregação read-only por produto: receita bruta, custo (landed), margem de contribuição e margem líquida operacional;
- custos desconhecidos permanecem `unknown` e nunca viram zero; avaliação incompleta não define margens;
- reproduz o mesmo contrato fail-closed do avaliador pré-publicação (politica explícita, sem defaults implícitos);
- painel sanitizado: nunca expõe supplier_sku_id, external ids, tokens ou payload bruto;
- rota protegida por `@admin_required`, exclusivamente GET.

## FASE 8.3.0 — Pós-venda, rastreio e exceções

**Status:** em andamento — checkpoints A–H implementados em branch; fila de exceções e alertas permanecem pendentes.

Objetivo: fechar o ciclo após a compra no fornecedor.

Base já existente reconciliada: a sincronização monotônica de status do
fornecedor foi entregue anteriormente na FASE 8.0.8, com provider gated,
hard-off por padrão e estados canônicos até entrega/cancelamento.

### Checkpoint 8.3.0-A — contrato sanitizado de rastreio

Implementado:

- boundary read-only separado para consultar tracking por pedido remoto;
- provider padrão HARD-OFF, sem chamadas de rede;
- validação estrita de tracking code e carrier, sem texto livre;
- cardinalidade 0/1/N validada contra os pedidos remotos persistidos;
- rastreio ausente permanece pendente e nunca vira string vazia ou dado inventado;
- testes unitários do contrato, sanitização, cardinalidade e hard-off.

### Checkpoint 8.3.0-B — persistência transacional de tracking

Implementado:

- campos nullable por pedido remoto: tracking code, carrier e timestamp;
- migration 0015 idempotente, restrita ao schema pollomodas;
- persistência 0/1/N sob lock, vinculada pela posição durável;
- atualização idempotente e no-op enquanto tracking não está disponível;
- conflito de identidade falha fechado sem sobrescrever o código existente;
- validator PostgreSQL e testes de persistência/rollback lógico.

### Checkpoint 8.3.0-C — acompanhamento read-only pelo cliente

Implementado:

- projeção sanitizada dos fulfillments e pedidos remotos;
- integração à consulta existente por número do pedido + e-mail;
- exibição de estado, tracking code, carrier e data de atualização;
- tracking pendente é comunicado explicitamente;
- nenhuma identidade remota, attempt key, diagnóstico ou payload é exposto;
- a requisição do cliente nunca consulta o provider nem altera o pedido.

### Checkpoint 8.3.0-D — adapter DS explícito de tracking

Implementado:

- extração restrita a logistics number e logistics service no endpoint DS já provado;
- suporte às formas documentadas de logistics info sem persistir payload bruto;
- consulta 0/1/N na ordem das identidades remotas duráveis;
- cardinalidade e identidade validadas antes de qualquer chamada;
- payload ausente permanece pendente e resposta ambígua falha fechado;
- cliente obrigatório por injeção; provider default continua HARD-OFF.

### Checkpoint 8.3.0-E — orquestração gated de tracking

Implementado:

- gate estrito ALIEXPRESS_TRACKING_SYNC_ENABLED, habilitado somente com valor 1;
- gate desligado bloqueia antes de construir ou chamar provider;
- provider ou factory precisa ser injetado explicitamente mesmo com gate ligado;
- elegibilidade limitada a estados pós-submissão com identidades remotas;
- consulta e persistência compostas sem retry automático;
- erros do provider são convertidos em reason codes sanitizados;
- tracking pendente conclui como no-op, sem inventar valores.

### Checkpoint 8.3.0-F — máquina de cancelamento e rejeição

Implementado:

- pedido de cancelamento somente para submitted/confirmed;
- cancel_requested idempotente e sem chamada remota;
- desfechos canônicos cancelled/rejected/unknown;
- cancelamento confirmado persiste cancelled_at sem sobrescrever timestamp;
- rejeição de cancelamento vai para manual_review;
- resultado ambíguo permanece cancel_requested, sem retry;
- rejeição conclusiva do fornecedor usa reason code fixo e sanitizado;
- estados shipped/delivered/cancelled não são reabertos silenciosamente.

### Checkpoint 8.3.0-G — boundary/preflight de reembolso

Implementado:

- elegibilidade somente quando todos os fulfillments estão cancelled;
- Order precisa continuar paid e possuir exatamente um pagamento approved;
- valor e moeda precisam coincidir integralmente com o pedido;
- gate estrito SUPPLIER_CANCELLATION_REFUND_ENABLED=1;
- provider padrão hard-off e provider real sempre explícito;
- confirmed/rejected/unknown não falsificam Order.status;
- refunded continua sendo confirmado pela autoridade existente: webhook Mercado Pago;
- nenhuma chamada financeira, retry ou mudança de estoque automática.

### Checkpoint 8.3.0-H — ledger e reconciliação financeira

Implementado:

- ledger em Order para requested/pending_webhook/confirmed/failed/manual_review;
- timestamps de solicitação e confirmação, sem sobrescrita em reprocessamento;
- migration 0016 idempotente e restrita ao schema pollomodas;
- outcomes do boundary persistidos sem alterar Order.status para refunded;
- webhook Mercado Pago confirma o ledger na mesma transação de refunded;
- refunds não relacionados a cancelamento do fornecedor não criam ledger;
- erros persistidos somente como códigos fixos e sanitizados.

Pendente para os próximos checkpoints: fila administrativa de exceções e
alertas de estagnação.

Critérios de saída:

- sincronização de status do fornecedor;
- tracking code/carrier persistidos quando disponíveis;
- cliente consegue acompanhar pedido;
- fluxo explícito para fornecedor rejeitar/cancelar pedido;
- fluxo de cancelamento/reembolso integrado ao estado interno;
- pedidos presos possuem fila/admin de exceção;
- alertas para fulfillment sem atualização por período definido.

## FASE 9.0.0 — Aquisição e crescimento

**Status:** futura, somente após o ciclo operacional estar estável.

Objetivo: transformar infraestrutura de venda em faturamento recorrente.

Frentes planejadas:

- Meta Ads / Instagram;
- TikTok orgânico e pago;
- Google Shopping quando catálogo/feed estiver pronto;
- SEO de páginas de produto/categoria;
- recuperação de checkout/pedido pendente;
- remarketing;
- métricas por canal: sessões, conversão, CAC, ticket médio, margem por pedido e recompra.

## Regra de progressão

Não avançar tráfego pago relevante enquanto uma venda aprovada ainda exigir correção manual para chegar ao fornecedor. Crescimento só deve ser acelerado depois de checkout, fulfillment, margem e pós-venda estarem observáveis e previsíveis.

## Regras permanentes de segurança AliExpress

- nenhuma autorização de merge implica autorização de chamada ao provider;
- nenhuma autorização de provider implica retry;
- resultado remoto ambíguo deve falhar fechado;
- nenhuma fase deve expor token, CPF, payload integral, attempt key ou remote ID desnecessário em logs;
- qualquer mudança que habilite compra real precisa de gates e testes próprios antes de produção.
