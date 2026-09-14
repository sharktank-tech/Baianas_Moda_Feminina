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

**Status:** implementação técnica concluída — checkpoints A–J entregues; migrations 0015/0016/0017 aplicadas e validadas no PostgreSQL de produção.

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
- migration 0016 idempotente, restrita ao schema pollomodas;
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
- migration 0017 idempotente e restrita ao schema pollomodas;
- outcomes do boundary persistidos sem alterar Order.status para refunded;
- webhook Mercado Pago confirma o ledger na mesma transação de refunded;
- refunds não relacionados a cancelamento do fornecedor não criam ledger;
- erros persistidos somente como códigos fixos e sanitizados.

### Checkpoint 8.3.0-I — fila administrativa de exceções

Implementado:

- rota GET admin-only em /admin/integrations/aliexpress/pedidos/exceptions;
- consolidação de manual_review, pending_reconciliation e failed;
- cancelamento e refund pendentes/rejeitados entram com reason codes fixos;
- projeção sanitizada sem remote order ID, attempt key, payload ou erro bruto;
- acesso direto ao detalhe interno do fulfillment para tratamento operacional;
- configuração inválida bloqueia a fila em vez de ocultar exceções.

### Checkpoint 8.3.0-J — alertas de estagnação

Implementado:

- janela FULFILLMENT_STALE_AFTER_HOURS, padrão 48h;
- validação estrita entre 1 e 720 horas;
- alertas para status, cancelamento e refund sem atualização;
- shipped sem tracking gera tracking_missing;
- timestamp ausente é tratado como exceção, nunca como registro recente;
- painel mostra totais, idade e motivos, sem polling ou chamadas externas.

Pendência operacional pós-merge: aplicar e validar migrations no PostgreSQL
antes de depender dos novos campos em produção. **Resolvido em 2026-09-13**:
0015 (snapshot de custo, FASE 8.2.0-D), 0016 (tracking remoto) e 0017 (ledger
de refund) aplicadas e validadas no PostgreSQL de produção (schema pollomodas).

Critérios de saída:

- sincronização de status do fornecedor;
- tracking code/carrier persistidos quando disponíveis;
- cliente consegue acompanhar pedido;
- fluxo explícito para fornecedor rejeitar/cancelar pedido;
- fluxo de cancelamento/reembolso integrado ao estado interno;
- pedidos presos possuem fila/admin de exceção;
- alertas para fulfillment sem atualização por período definido.

## FASE 9.0.0 — Aquisição e crescimento

**Status:** iniciada. Frente base (coleta de origem e métricas por canal)
entregue; tráfego pago permanece gated pela regra de progressão abaixo.

Objetivo: transformar infraestrutura de venda em faturamento recorrente.

Frentes planejadas:

- Meta Ads / Instagram;
- TikTok orgânico e pago;
- Google Shopping quando catálogo/feed estiver pronto;
- SEO de páginas de produto/categoria;
- recuperação de checkout/pedido pendente;
- remarketing;
- métricas por canal: sessões, conversão, CAC, ticket médio, margem por pedido e recompra.

Checkpoints entregues:

- **9.0.0-A (2026-09-14):** captura sanitizada de origem de tráfego (UTM/
  referrer first-touch) no pedido — migration 0018, colunas acquisition_*
  opcionais em orders, allowlist estrita e fail-closed. Nunca persiste payload
  bruto da URL.
- **9.0.0-B (2026-09-14):** dashboard administrativo somente leitura de métricas
  por canal (`/admin/metricas-por-canal`) — pedidos, conversão, ticket médio,
  margem por pedido (avaliador 8.2.0-C) e recompra, agrupados por origem. Sem
  sessões persistidas não há "sessões"; sem gasto publicitário não há CAC — os
  campos permanecem ausentes em vez de inventados.
- **9.0.0-C (2026-09-14):** recuperação de checkout/pedido pendente por e-mail
  (sem tráfego pago) — fila de pedidos `pending_payment` com reserva ativa,
  idade mínima configurável (`CHECKOUT_RECOVERY_AFTER_HOURS`) e lembrete único
  por pedido (dedupe via `recovery_email_sent_at`, migration 0019). Envio
  fail-closed e gated por `CHECKOUT_RECOVERY_ENABLED=1`; coluna nunca cai em
  `public`; disparo via webhook/retorno a partir dos touchpoints de rotina e
  botão admin em `/admin/recuperacao-checkout`.
- **9.0.0-D (2026-09-14):** SEO de páginas de produto/categoria — JSON-LD
  Product com oferta/esgotado/inventário, `<link rel="canonical">` em produto e
  listagem, title/meta dinâmicos e `noindex` para busca, `sitemap.xml` público
  (produtos ativos + páginas institucionais) e `Sitemap:` no `robots.txt`.
- **9.0.0-E (2026-09-14):** hardening da recuperação e SEO — formulário
  administrativo protegido por CSRF; JSON-LD serializado com `tojson` contra
  encerramento de `<script>`; fila limitada a 50 pedidos e somente reservas
  dentro da janela real de 24h; execução concorrente usa
  `FOR UPDATE SKIP LOCKED`. O envio em lote foi removido do webhook e do
  retorno do Mercado Pago, ficando restrito à ação administrativa gated.
  A deduplicação é concorrente/best-effort: SMTP e banco não oferecem transação
  distribuída, portanto queda entre envio e commit ainda pode exigir auditoria.
- **9.0.0-F (2026-09-14):** feed Google Merchant preparado em RSS 2.0,
  somente leitura e hard-off por `GOOGLE_MERCHANT_FEED_ENABLED=1`. Exporta
  apenas campos públicos de produtos ativos/não rascunho com preço BRL positivo
  e imagem HTTP(S); valores inválidos são omitidos, nunca convertidos em zero.
  A rota `/feeds/google-merchant.xml` responde 404 enquanto gated. Nenhum feed
  foi cadastrado no Google e nenhuma campanha ou chamada externa foi feita.
- **9.0.0-G (2026-09-14):** sessões agregadas por dia/canal para medir
  conversão real (pagamentos aprovados / sessões). O banco guarda somente
  `day`, canal sanitizado e contador; não persiste IP, URL, cookie, usuário ou
  identificador publicitário. Cada sessão Flask incrementa no máximo uma vez
  por dia em páginas públicas de catálogo, usando upsert atômico. Dashboard e
  pedidos são alinhados ao início da janela medida. Migration 0020 aplicada e
  validada em produção em 2026-09-14, exclusivamente no schema `pollomodas`.
- **9.0.0-H (2026-09-14):** hardening de acesso ao banco após auditoria
  dos grants. O schema `pollomodas` não concede `USAGE` nem privilégios sobre
  relações/funções a `anon`, `authenticated`, `service_role` ou
  `authenticator`; a aplicação usa conexão PostgreSQL de backend. Migration
  0021 torna essas revogações explícitas também para objetos futuros, sem
  habilitar RLS em massa e sem alterar o schema legado `public`. A migration
  foi aplicada e validada no PostgreSQL de produção em 2026-09-14, registrada
  como `20260914140452 private_pollomodas_schema_0021`.
- **Validação operacional 9.0.0-H (2026-09-14):** durante o smoke pós-migration,
  a home e a listagem retornaram HTTP 500 por credencial desatualizada na
  `DATABASE_URL` da Vercel. Os logs provaram que a falha de autenticação do
  usuário `postgres` era preexistente (com ocorrências desde 2026-07-03), não
  causada pelos grants da migration. Após atualizar a variável e o deployment,
  home, `/produtos`, detalhe de produto e retorno à listagem responderam HTTP
  200, sem erro visual ou novos HTTP 500 na janela final de observação. A
  persistência agregada de sessões no schema privado também foi confirmada; os
  acessos públicos continuam sem privilégios diretos ao schema.

## Regra de progressão

Não avançar tráfego pago relevante enquanto uma venda aprovada ainda exigir correção manual para chegar ao fornecedor. Crescimento só deve ser acelerado depois de checkout, fulfillment, margem e pós-venda estarem observáveis e previsíveis.

**Incidente Mercado Pago resolvido (2026-09-14):** a issue #4 foi encerrada
após a tentativa LIVE controlada de 2026-09-10 materializar um `Payment`
`approved`, persistir o pagamento e promover o pedido para `paid`. A
revalidação read-only em produção confirmou 1 pedido pago e 1 pagamento
aprovado. A issue #5 foi encerrada como duplicata.

**Pendência residual:** a issue #50 acompanha notificações legadas do Mercado
Pago rejeitadas com HTTP 401. Os webhooks atuais foram aceitos com HTTP 200 e
foram suficientes para a reconciliação do pagamento. A revisão deve preservar
validação fail-closed; não autoriza aceitar webhook sem autenticidade
comprovada nem executar nova compra LIVE.

Tráfego pago relevante e cadastro externo do feed continuam sujeitos à regra
de progressão: somente avançar depois que checkout, fulfillment, margem e
pós-venda estiverem observáveis e previsíveis.

## Regras permanentes de segurança AliExpress

- nenhuma autorização de merge implica autorização de chamada ao provider;
- nenhuma autorização de provider implica retry;
- resultado remoto ambíguo deve falhar fechado;
- nenhuma fase deve expor token, CPF, payload integral, attempt key ou remote ID desnecessário em logs;
- qualquer mudança que habilite compra real precisa de gates e testes próprios antes de produção.
