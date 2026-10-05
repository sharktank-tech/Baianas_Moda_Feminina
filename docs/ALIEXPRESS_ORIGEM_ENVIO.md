# AliExpress — Origem de envio e sincronização técnica (Etapas 1–3)

**Status:** implementado na branch `feature/aliexpress-origem-envio`
(commits `0ecb9ee`, `1aea6e0`, `ef0176d`). Nenhum merge, deploy, compra,
pedido real ou cron foi criado por este trabalho. Nada aqui despublica um
produto já à venda.

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
  código estável) sem ser aplicada; nunca gera `stale` nem reprecificação.

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
| Novos SKUs entram **inativos** (nunca à venda sozinhos) | `pricing_status`/`pricing_error` (nunca vira `stale` sozinho) |
| `Product.stock` (soma das variantes ativas) | `is_draft` / `is_active` — **nunca despublica** |
| `sync_status` / `last_sync_at` / `sync_error` (produto, vínculo) | frete, landed cost e evidência de cotação |
| `ProductSync.last_synced_at` / `error` (`state` preservado) | origem de envio e seleção de operação nacional |
| — | atributos/cor/tamanho/preço das variantes existentes; variante desativada nunca é reativada |

Divergências sinalizadas em `sync_error`: `supplier_cost_changed`,
`currency_changed` (aviso no flash: *divergência sinalizada SEM aplicar*).
Conflitos controlados abortam **antes** de qualquer escrita:
`not_published`, `external_id_mismatch`, `link_mismatch`,
`missing_supplier_price`, `missing_currency`, `currency_mismatch`,
`variant_label_too_long`.

---

## 5. Como aplicar e operar

1. **Migração**: `migrations/0034_shipping_origin_evidence.sql` (e
   `0034_shipping_origin_evidence_down.sql` para reverter). Idempotente
   (`CREATE TABLE IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`), schema
   `pollomodas`, aplica-se com a ferramenta de migração usual do projeto.
   A coluna nova nasce `FALSE` (sem backfill): nenhum produto é considerado
   nacional só por causa da migração.
2. **Após aplicar**: nenhum passo manual adicional. Rota de catálogo passa a
   exibir o painel de importados e os selos de origem.
3. **Rotina de origem**: importar → cotar frete com evidência → precificar →
   (opcional) confirmar origem por variante com evidência → marcar variantes
   da operação nacional → publicar. Publicar sem origem comprovada continua
   permitido **enquanto nenhuma variante estiver marcada como operação
   nacional**.
4. **Rotina de sincronização**: na página do produto publicado, *Sincronizar
   estoque agora*. Revisar o aviso de divergência de custo/moeda antes de
   renovar frete/reprecificar (essa decisão continua explícita).
5. **Rollback**: reverter a migração desativa os dados novos; o produto
   publicado permanece ativo (o gate deixa de valer quando a coluna/cadastro
   não existe mais).

---

## 6. Testes e pendências

Suítes relevantes (venv `venv-payment-guard`, sem rede):

- `tests/test_shipping_origin_evidence.py` — serviço, modelo e CHECKs (39);
- `tests/test_shipping_origin_pg.py` — ida/venida da migração em PostgreSQL
  descartável (só roda com PG local);
- `tests/test_admin_sales_readiness.py`, `tests/test_publication.py` — gates
  de origem nos dois níveis;
- `tests/test_aliexpress_admin_catalog.py` — rotas de origem/nacional,
  exibição honesta, filtro de importados, validação de entrada, logs;
- `tests/test_published_product_sync.py` — sincronização técnica (15).

**Pendências declaradas**

1. Testes `*_pg.py` (migração em PostgreSQL) **não rodam neste ambiente**
   (não há Postgres local; a URL do `.env` aponta para o banco remoto e a
   checagem de segurança `IS_SAFE_POSTGRES` os pula). Executar em CI/local com
   PG descartável antes do merge.
2. `record_api_observation()` permanece sem chamador em produção até haver
   observação real de `ship_from_country` (limitação 2 da seção 2).
3. Novos SKUs detectados por sincronização entram **inativos**; a ativação de
   uma nova opção de venda é decisão comercial manual (ainda não há tela para
   isso).
4. Nenhum merge/deploy sem aprovação; mudança visível (painel de importados,
   selos, botões) exige aprovação prévia.
