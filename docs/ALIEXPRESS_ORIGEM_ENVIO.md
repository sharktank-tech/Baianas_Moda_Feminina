# AliExpress — Origem de envio e sincronização técnica (Etapas 1–3)

**Status:** implementado na branch `feature/aliexpress-origem-envio`
(commits `0ecb9ee`, `1aea6e0`, `ef0176d` + correções da revisão do PR #123).
Nenhum merge, deploy, compra, pedido real ou cron foi criado por este
trabalho. Nada aqui despublica um produto já à venda.

---

## 0. Revisão do PR #123 — o que foi corrigido

| # | Ponto da revisão | Onde ficou |
| --- | --- | --- |
| 1 | Divergência de custo/moeda **invalida a aprovação operacional** e **bloqueia novas compras** pelos controles canônicos, preservando preço/conteúdo | `loja/services/purchase_controls.py`, `sales_readiness.py` (`supplier_cost_divergence`/`supplier_currency_divergence`), gates em `_reserve_stock` e `_validate_cart_for_checkout`, `pricing_status='stale'` (migração `0035`) |
| 2 | Origem nacional validada **por variante no checkout**, incluindo confirmação removida **depois** da publicação | `purchase_controls.national_origin_code()` + mesmos dois gates de checkout |
| 3 | Reconciliação de SKUs não presume completude pela presença de SKUs | `published_sync.catalog_census()` (`CatalogCensus`): só contagem/agregado **declarado** e conferente autoriza desativar SKU ausente; sem prova ⇒ `catalog_complete=False` + `catalog_code` |
| 4 | Sync de estoque integrado a **locks + contabilidade de reservas**, com teste de concorrência em PostgreSQL | `published_sync._locked_variants()` (ordem Product → ProductVariant, igual ao checkout), `stock_reservations.reserved_quantities_by_variant()`, `tests/test_published_sync_concurrency_pg.py` |
| 5 | Confirmação coletiva (`covers_all_variants`) restrita aos SKUs **cobertos no momento da escrita** | coluna `covered_supplier_sku_ids` (migração `0036`), snapshot em `record_manual_confirmation`/`record_api_observation`, `shipping_origin._covers_sku()` |
| 6 | Tentativas/falhas de sincronização **persistidas** (não só flash) | tabela `aliexpress_sync_attempts` (migração `0037`), `AliExpressSyncAttempt`, seção *Tentativas recentes* na página de renovação |
| 7 | `ae_item_base_info_dto.total` **nunca** conta como total de SKUs | `published_sync._declared_sku_total()`: chaves pareadas com a **fonte** (`total`/`count` só dentro de `ae_item_sku_info_dtos`); regressão em `tests/test_published_sync_resilience.py` |
| 8 | Sync **recarrega** produto/variante/vínculo **depois** dos locks | `published_sync._lock_and_reload()` (`populate_existing()` + `refresh()`), chamado ANTES de ler estoque/divergência; teste PG `test_sync_aguarda_checkout_e_recarrega_registros` (a sync espera o checkout) |
| 9 | Liberação de reserva **nunca infla acima do estoque do fornecedor** | coluna `product_variants.supplier_stock` (migração `0038` + `loja/schema.py`), teto `min(atual+qtd, S − outras reservas)` em `stock_reservations._restock_variant()` |
| 10 | Os 2 testes de cotação de frete deixam de **pular** em PostgreSQL | `tests/test_supplier_freight_quotes_pg.py` lê `ORIGINAL_DATABASE_URL` do conftest (guards de loopback/`pollo*` mantidos) |

---

## 1. O que foi implementado, por etapa

### Etapa 1 — origem de envio por variante com evidência

- `migrations/0034_shipping_origin_evidence.sql`
  - tabela `pollomodas.supplier_sku_shipping_origins` com **colunas separadas**
    para classificação manual (`manual_origin`, `evidence_ref`,
    `confirmed_at`, `confirmed_by_user_id`) e para observação de dado do
    fornecedor (`api_origin`, `api_observed_at`);
  - **escopo XOR** por linha (`supplier_sku_id` **ou** `covers_all_variants`,
    com CHECK) e índices únicos parciais (um registro por SKU, um de cobertura);
  - coluna `product_variants.national_shipping_operation`
    (`BOOLEAN NOT NULL DEFAULT FALSE`, sem backfill) + entrada em
    `loja/schema.py` (`REQUIRED_COLUMNS`).
- `loja/services/shipping_origin.py` — resolução **fail-closed**:
  - ausência de linha = desconhecido (nunca é tratado como Brasil);
  - precedência `SKU > cobertura total`; conflito `manual × api` na mesma
    linha é sinalizado, nunca silenciado; a API **nunca** sobrescreve a
    confirmação manual;
  - resumo do produto `brazil | foreign | unknown | mixed | conflict`
    (1 SKU BR + 1 desconhecida ⇒ `mixed`, **nunca** nacional);
  - gate `national_operation_reasons()` só enxerga variantes **ativas e
    selecionadas**.
- Gates: códigos estáveis `national_origin_unproven`,
  `national_origin_foreign`, `national_origin_conflict` (BLOCKING) em
  `loja/services/sales_readiness.py` e em `loja/services/publication.py`.
  `evaluate_publication()` bloqueia a publicação/republicação, mas **nunca
  despublica** o que já está ativo.

### Etapa 2 — busca administrativa e seleção de operação nacional

- Rotas (todas `@admin_required` + sessão AliExpress):
  `POST /admin/integrations/aliexpress/{rascunho,publicado}/<id>/origem` e
  `POST .../{rascunho,publicado}/<id>/nacional`.
- Validação **antes** de qualquer escrita, escopo XOR, `evidence_ref`
  obrigatória (máx. 200), transação única com `commit` único e rollback em
  falha; limpar a confirmação preserva qualquer observação da API.
- Catálogo: painel **Produtos importados** (resumo em lote, uma consulta),
  chips de filtro `?origem=brazil|foreign|unknown|mixed|conflict` (valor
  inválido é ignorado), teto de 50 itens.
- Exibição honesta: resultados de busca e página de detalhe **não** nascem
  como "Brasil"; sem produto importado com evidência o selo é sempre
  *Origem não verificada*.
- `loja/templates/admin/aliexpress/_origin_fields.html` (parcial compartilhado
  por rascunho e renovação de frete).

### Etapa 3 — sincronização técnica de produto publicado

- `loja/integrations/aliexpress/published_sync.py` + rota
  `POST /admin/integrations/aliexpress/publicado/<id>/sincronizar`
  (botão **Sincronizar estoque agora** na página de renovação).
- Atualiza **somente** estoque/dados técnicos a partir do detalhe já obtido
  pela rota: o módulo não tem credenciais, não constrói cliente e não faz
  nenhuma chamada de frete, compra ou pedido.
- Divergência de custo/moeda é comparada e **sinalizada** (`sync_error` com
  código estável) sem ser aplicada ao custo, à moeda, ao preço ou à margem;
  ela **invalida a aprovação operacional** (`pricing_status='stale'` com o
  código em `pricing_error`, aceito pela guarda da migração `0035`) e é
  bloqueada para **nova compra** por `loja/services/purchase_controls.py`.
  República exige `priced` de novo: o admin resolve renovando/reprecificando.

---

## 2. Campos da API que podem comprovar origem — referência e limitações

Referência oficial: **AliExpress Open Platform — API Reference**
(<https://openservice.aliexpress.com/doc/api.htm>); o roteador TOP documenta
os parâmetros das queries de frete (ex.: `ship_from_country` — *"Country to
ship from"*, `ship_to_country` — *"Country to ship to"*).

| Campo | Onde aparece | O que é, de fato | Uso aqui |
| --- | --- | --- | --- |
| `ship_from_country` | resposta/opção de frete; nas docs oficiais também aparece como **parâmetro de requisição** da query de frete | país de origem do envio **pedido/cotado**, quando presente na resposta | único campo de origem consumido (`freight.py`, `_opt(option, "ship_from_country", "dispatchCountry", "country", "countryCode")`) |
| `send_goods_country_code` | **requisição** de `buyer.freight.calculate` (default `CN`) | país do qual *pedimos* a cotação | nunca é prova de origem; não é lido como evidência |
| `ship_to_country` / `countryCode` (product.get, search) | requisição | **destino** do comprador/preço local | ignorado como origem |

**Limitações declaradas**

1. No `freight.py`, o `country` da opção de frete é preenchido com *fallback*
   (`ship_from_country` → `dispatchCountry` → `country` → `countryCode`); os
   últimos nomes são ambíguos (podem ser destino). Por isso qualquer valor
   observado ali só entra na coluna `api_origin` como **observação**, nunca
   como prova de Brasil.
2. O frete por resposta está **bloqueado para App Key** (`null`/`501`/`505` em
   produção) e os testes do catálogo administrativo **proíbem chamadas de
   frete**. Consequência honesta: hoje `record_api_observation()` **não tem
   chamador em produção** — nenhuma linha `api_origin` é gravada por esse
   caminho. Ele existe documentado para quando houver observação real.
3. O payload bruto do detalhe do produto (`ae_item_base_info_dto` e cia.) é
   normalizado e **não é persistido**; não é possível inspecioná-lo depois
   para achar um campo de origem retroativamente.
4. Portanto, **hoje a origem só entra por confirmação manual com evidência**.
   Enquanto não houver evidência, o produto permanece *não verificado* e a
   operação nacional selecionada fica bloqueada.

---

## 3. Como funciona a confirmação manual

1. O admin abre **Catálogo → produto importado → rascunho** (ou a página do
   produto publicado).
2. Em **Origem de envio** escolhe o **escopo**: *uma variante específica* ou
   *Todas as variantes (cobertura explícita)* — nunca os dois (XOR).
3. Escolhe **Brasil** ou **Exterior**, informa o país (2 letras, opcional) e a
   **referência da evidência** (link do anúncio, print do vendedor, número de
   pedido) — campo obrigatório; sem ele nada é gravado.
4. A linha é gravada em transação única. **Limpar confirmação** remove a
   linha manual e preserva qualquer observação da API.
   A **cobertura explícita** grava, no momento da escrita, quais SKUs ela
   cobre (`covered_supplier_sku_ids`, migração `0036`): um SKU que nascer
   depois (sincronização) **não herda** a origem BR — continua desconhecido
   até o admin reconfirmar (a reconfirmação redeclara o escopo) ou gravar
   uma linha própria.
5. Em **Operação nacional** o admin marca quais variantes ativas participam da
   operação nacional. A seleção é explícita e reversível.
6. Efeito: cada variante **selecionada** precisa de origem **Brasil
   comprovada** para publicar. Os bloqueios aparecem no rascunho como
   *Bloqueios de operação nacional ativos* com os códigos estáveis
   `national_origin_unproven` / `national_origin_foreign` /
   `national_origin_conflict`. Variantes **não selecionadas** nunca bloqueiam
   (catálogo internacional continua funcionando).
7. `record_api_observation()` é o único caminho da coluna `api_origin` e
   (limitação 2 acima) ainda não é chamado por produção.

---

## 4. Sincronização de produto publicado — atualizado × preservado

| Atualizado (técnico/estoque) | Preservado (decisão comercial — nunca escrito) |
| --- | --- |
| `ProductVariant.stock` (SKU do payload) | `Product.name`, `description`, `image1..4` + galeria, `category`/`category_id` |
| Desativação de variante com estoque `0` ou SKUs sumidos do catálogo | `Product.price`, `target_margin_percent`, `currency`, `cost` |
| Novos SKUs entram **inativos** (nunca à venda sozinhos) | `pricing_status`/`pricing_error` (só muda para `stale` quando há **divergência sinalizada**; a sync nunca reprecifica) |
| `Product.stock` (soma das variantes ativas), sempre `fornecedor − reservas` | `is_draft` / `is_active` — **nunca despublica** |
| Desativação de SKU ausente **só com completude comprovada** (`catalog_code`) | frete, landed cost e evidência de cotação |
| `sync_status` / `last_sync_at` / `sync_error` (produto, vínculo) | origem de envio e seleção de operação nacional |
| `ProductSync.last_synced_at` / `error` (`state` preservado) | origem de envio e seleção de operação nacional |
| **Toda tentativa** gravada em `aliexpress_sync_attempts` (`applied`/`rejected`/`failed`/`api_error`) | atributos/cor/tamanho/preço das variantes existentes; variante desativada nunca é reativada |

Divergências sinalizadas em `sync_error`: `supplier_cost_changed`,
`currency_changed` (aviso no flash: *divergência sinalizada SEM aplicar*).
Efeito da divergência em produto publicado: preço, margem, conteúdo e
publicação **ficam intactos**, a precificação fica `stale` e
`purchase_controls` bloqueia nova compra (códigos
`supplier_cost_divergence`/`supplier_currency_divergence` no readiness e no
checkout) até o admin resolver.

Completude do catálogo (`catalog_census`, fail-closed): a presença de SKUs
nunca prova que a resposta é completa. Só a **contagem declarada**
(`ae_item_sku_info_dtos.sku_count` e cia.) ou o **agregado declarado**
(`available_stock`) conferindo com o observado autoriza desativar SKUs
ausentes; sem prova, `catalog_complete=False` com
`catalog_census_empty`/`catalog_census_unproven`/`catalog_sku_count_mismatch`/
`catalog_stock_mismatch` — os SKUs presentes atualizam normal e nada é
desativado (aviso no flash e `catalog_code` na tentativa persistida).

Estoque: as variantes são travadas (`SELECT ... FOR UPDATE`) na **mesma
ordem do checkout** (Product → ProductVariant) e o valor absoluto aplicado é
`estoque do fornecedor − reservas locais ainda debitadas` — pedido pendente
nunca tem estoque devolvido por sync, e pedido já **submetido** ao fornecedor
não é debitado duas vezes.

Conflitos controlados abortam **antes** de qualquer escrita:
`not_published`, `external_id_mismatch`, `link_mismatch`,
`missing_supplier_price`, `missing_currency`, `currency_mismatch`,
`variant_label_too_long`.

---

## 5. Como aplicar e operar

1. **Migrações** (todas aditivas, idempotentes, schema `pollomodas`, com
   `_down.sql` correspondente e aplicáveis com a ferramenta usual do projeto):
   - `0034_shipping_origin_evidence.sql` — origem por variante com evidência;
     a coluna nova nasce `FALSE` (sem backfill): nenhum produto é considerado
     nacional só por causa da migração;
   - `0035_published_divergence_guard.sql` — permite `pricing_status='stale'`
     em produto AliExpress **publicado** (divergência sinalizada);
     `unpriced`/`blocked` em produto ativo continua rejeitado. O `_down`
     volta a exigir `priced` (não execute com produto publicado `stale`);
   - `0036_shipping_origin_cover_snapshot.sql` — coluna
     `covered_supplier_sku_ids` (sem backfill: `NULL` = nada coberto);
    - `0037_aliexpress_sync_attempts.sql` — tabela + índice de tentativas;
    - `0038_variant_supplier_stock.sql` — coluna
      `product_variants.supplier_stock` (espelho do total informado pelo
      fornecedor; nasce `NULL`, **sem backfill**: só a próxima sincronização
      preenche e a liberação de reserva só passa a ter teto a partir daí).
2. **Após aplicar**: nenhum passo manual adicional. Rota de catálogo passa a
   exibir o painel de importados e os selos de origem; a página de renovação
   passa a mostrar *Tentativas recentes*.
3. **Rotina de origem**: importar → cotar frete com evidência → precificar →
   (opcional) confirmar origem por variante com evidência → marcar variantes
   da operação nacional → publicar. Publicar sem origem comprovada continua
   permitido **enquanto nenhuma variante estiver marcada como operação
   nacional**. A confirmação coletiva vale só para os SKUs existentes naquele
   momento (reconfirmar inclui os novos).
4. **Rotina de sincronização**: na página do produto publicado, *Sincronizar
   estoque agora*. Divergência de custo/moeda **bloqueia a venda** enquanto
   durar (produto fica `stale`, checkout recusa): renove frete/reprecifique
   para resolver; a decisão de preço continua explícita. Recusa/falha fica
   registrada em *Tentativas recentes*.
5. **Rollback**: reverter as migrações na ordem inversa desativa os dados
   novos; o produto publicado permanece ativo (o gate deixa de valer quando a
   coluna/cadastro não existe mais). Com `0035` revertido, um produto
   publicado `stale` passa a violar a guarda — resolva a divergência antes.

---

## 6. Testes e pendências

Suítes relevantes (venv `venv-payment-guard`, sem rede):

- `tests/test_shipping_origin_evidence.py` — serviço, modelo e CHECKs (39);
- `tests/test_shipping_origin_pg.py` — ida/venida das migrações `0034`/`0036`
  em PostgreSQL descartável (só roda com PG local);
- `tests/test_admin_sales_readiness.py`, `tests/test_publication.py` — gates
  de origem nos dois níveis;
- `tests/test_aliexpress_admin_catalog.py` — rotas de origem/nacional,
  exibição honesta, filtro de importados, validação de entrada, logs;
- `tests/test_published_product_sync.py` — sincronização técnica (15);
- `tests/test_aliexpress_checkout_guards.py` — **bloqueios no fluxo real de
  checkout** (abertura do carrinho + criação do pedido): divergência de
  custo/moeda, origem por variante e confirmação removida após publicação,
  com pedido real criado (ou recusado) nos controles;
- `tests/test_published_sync_resilience.py` — completude não provada (inclusive
  `total` do `ae_item_base_info_dto` que **não** conta como SKUs), reservas na
  sincronização, teto da liberação após queda do fornecedor, persistência de
  tentativas e cobertura de SKUs;
- `tests/test_published_sync_concurrency_pg.py` — **concorrência** sync ×
  checkout × release em PostgreSQL (locks + reservas), inclusive o caminho em
  que a **sync espera o checkout** e recarrega os objetos sob o lock;
- `tests/test_aliexpress_sync_attempts_pg.py` — migração `0037` em PostgreSQL;
- `tests/test_variant_supplier_stock_pg.py` — migração `0038` (coluna
  `supplier_stock`) em PostgreSQL;
- `tests/test_pricing_publication_pg.py` — migrações `0005`/`0035` (guarda de
  publicação com `stale`) em PostgreSQL.

**Pendências declaradas**

1. Testes `*_pg.py` (migração e concorrência em PostgreSQL) **não rodam
   neste ambiente** (não há Postgres local; a URL do `.env` aponta para o
   banco remoto e a checagem de segurança `IS_SAFE_POSTGRES` os pula).
   Eles estão na lista dos jobs do CI (`.github/workflows/pr14-pytest.yml`):
   `pytest-postgres` (`test_shipping_origin_pg.py`,
   `test_aliexpress_sync_attempts_pg.py`,
   `test_variant_supplier_stock_pg.py`,
   `test_published_sync_concurrency_pg.py`,
   `test_supplier_freight_quotes_pg.py`) e `pytest-pricing-pg`
   (`test_pricing_publication_pg.py`); localmente continue executando com um
   PG descartável `pollo*` em loopback.
2. `record_api_observation()` permanece sem chamador em produção até haver
   observação real de `ship_from_country` (limitação 2 da seção 2).
3. Novos SKUs detectados por sincronização entram **inativos**; a ativação de
   uma nova opção de venda é decisão comercial manual (ainda não há tela para
   isso).
4. Resolução de divergência de **moeda** em produto publicado não tem tela
   própria: o bloqueio permanece até o admin resolver com a rotina de
   reprecificação (fora do escopo desta revisão).
5. Nenhum merge/deploy sem aprovação; mudança visível (painel de importados,
   selos, botões, *Tentativas recentes*) exige aprovação prévia.
