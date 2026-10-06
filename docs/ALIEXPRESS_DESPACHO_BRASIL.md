# Investigação — descoberta de produtos AliExpress com despacho do Brasil

**Data:** 2026-10-06 · **Branch:** `investigate/aliexpress-despacho-brasil`
(base: merge do PR #123, `9308830`) · **Sem merge, deploy, publicação,
compras reais ou alteração de produção nesta entrega.**

Regra de negócio: a PolloModas venderá as duas origens. Nada aqui exclui,
suspende ou despublica produtos internacionais. "Origem" = **país de
despacho da variante** (não fabricação, vendedor ou destino).

---

## 1. Diagnóstico da busca atual

### Método efetivamente chamado

`AliExpressClient.search_products()`
(`loja/integrations/aliexpress/client.py:221`)
→ `build_search_params()` (`search.py:34`)
→ `call("aliexpress.ds.text.search", **business)`
→ `normalize_search_response()` (`search.py:215`).

### Parâmetros enviados (fixos no painel, rota `POST /buscar`, `aliexpress_admin_routes.py:931`)

| Parâmetro TOP | Valor enviado | Origem |
| --- | --- | --- |
| `keyWord` | termo do campo `q` (único input do painel) | formulário |
| `local` | `en_US` | `SEARCH_LOCALE` |
| `countryCode` | `BR` | `SEARCH_COUNTRY_CODE` |
| `currency` | `BRL` | `SEARCH_CURRENCY` |
| `sortBy` | `orders,desc` | fixo na rota |
| `pageIndex` | `1` | fixo na rota (sem paginação na UI) |
| `pageSize` | `20` | `SEARCH_PAGE_SIZE` |
| `categoryId` / `searchExtend` / `selectionName` | nunca enviados (`None`) | — |

### Filtros expostos no painel

Somente o campo de texto `q`. Não há na UI: filtro de país de despacho,
ordenação, categoria ou paginação. O filtro `?origem=` do catálogo atua
**somente sobre produtos já importados** (banco local) — não é busca
nacional no catálogo do fornecedor e não é apresentado como tal.

### Detalhe por ID

`AliExpressClient.get_product()` (`client.py:259`) → `aliexpress.ds.product.get`
com `product_id`, `ship_to_country="BR"`, `target_currency="BRL"`,
`target_language="pt"` (defaults; rotas `detalhe`/`importar` não alteram).
`ship_to_country` é **destino** (para preço/frete ao comprador BR).

### Referências oficiais verificadas

- `aliexpress.ds.product.get` — doc oficial TOP (apiId 60452, conferida em
  2026-10-06): request = `ship_to_country` ("Country"), `product_id`,
  `target_currency`, `target_language` (todos opcionais). **Nenhum parâmetro
  de origem de despacho.** Resposta (`AeItemQueryResultDTO`) =
  `ae_item_base_info_dto`, `ae_item_sku_info_dtos`, `ae_multimedia_info_dto`,
  `package_info_dto`, `logistics_info_dto` (**só `delivery_time` +
  `ship_to_country`, ou seja, destino**), `ae_item_properties`,
  `ae_store_info` (loja/avaliações — **não é despacho**),
  `product_id_converter_result` (`sub_product_id` = mapa país de
  **destino** → ID alternativo — **não é despacho**). Nenhum campo de
  despacho em nenhum ponto do schema oficial.
- `aliexpress.ds.text.search` — conjunto de parâmetros corroborado pelo
  código live, SDKs TOP e integrações DS de terceiros: `keyWord`, `local`,
  `countryCode`, `categoryId`, `sortBy`, `pageIndex`, `pageSize`,
  `currency`, `searchExtend`, `selectionName`. **Nenhum filtro oficial por
  país de despacho localizado**; `searchExtend`/`selectionName` não têm
  semântica documentada — não foram adotados (sem parâmetros por nome
  sugestivo). O `shipFromCountry`/`shipFrom` existe **só na busca pública
  do site** (filtro de vitrine), não na Open Platform DS.
- `aliexpress.ds.image.search` (apiId 60453): `shpt_to` = destino. Mesma
  conclusão.

### Há restrição de catálogo?

**Não.** Nenhum parâmetro enviado restringe por despacho:
`countryCode=BR` é país do **comprador** (preços em BRL para BR) e
`ship_to_country=BR` é **destino** — nenhum dos dois comprova nem filtra
origem brasileira. A ausência de produtos nacionais nos resultados se
explica por (a) **inexistência de filtro de despacho no método oficial** e
(b) ordenação fixa `orders,desc` (mais vendidos primeiro, majoritariamente
CN). Paginação existe na API (`pageIndex`/`pageSize`) e foi preservada no
cliente; só não é exposta na UI.

### Limitações que permanecem

1. Sem filtro oficial de despacho, a busca geral sempre mistura origens.
2. O detalhe oficial não devolve despacho por variante em campo
   documentado — a origem por variante continua dependendo de evidência
   externa + confirmação manual (fluxo da Etapa 1, inalterado).
3. `searchExtend`/`selectionName` seguem sem semântica comprovada.

---

## 2. Candidatos com despacho brasileiro (vitrine pública, somente leitura)

Método: busca pública filtrada por `shipFromCountry=BR`
(`Local de envio: Brasil` ativo), 2026-10-06, sem login e sem compra.
Cada cartão carrega telemetria `"shipFrom":"BR"` amarrada ao `skuId` do
cartão (SKU padrão do anúncio). **Evidência em nível de anúncio, não de
variante escolhida** — a página do anúncio exige login/JS para as opções
de envio por variante (ver §5), por isso **nenhum candidato é apresentado
como confirmado**.

Destino de teste: CEP **77000000** (Palmas — mesmo CEP de teste do repo).

| # | Anúncio (URL e ID) | Variante observada | Origem indicada | Preço exibido | Frete/prazo | Evidência |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `https://pt.aliexpress.com/item/1005013003211750.html` · ID `1005013003211750` — "Vestido Feminino 2026 com Estampa Floral, Alças Finas, Estilo Boho" | SKU padrão do cartão `12000059996865387` (tamanho/cor a escolher no anúncio) | `shipFrom: BR` na vitrine filtrada | R$ 69,85 | indisponível sem login | vitrine filtrada `shipFromCountry=BR`, 2026-10-06 |
| 2 | `https://pt.aliexpress.com/item/1005012926123897.html` · ID `1005012926123897` — "Vestidos de Verão Femininos, Vestido Maxi Floral" | SKU padrão do cartão `12000059755219799` | `shipFrom: BR` na vitrine filtrada | R$ 59,50 | indisponível sem login | vitrine filtrada `shipFromCountry=BR`, 2026-10-06 |
| 3 | `https://pt.aliexpress.com/item/1005010478234256.html` · ID `1005010478234256` — "Vestido Sem Manga em Algodão" | SKU padrão do cartão `12000052539612570` | `shipFrom: BR` na vitrine filtrada | R$ 34,80 (de R$ 58,00, −40%) | indisponível sem login | vitrine filtrada `shipFromCountry=BR`, 2026-10-06 |

Controle internacional (para a matriz do §3): os IDs sintéticos do repo
(`2000000000001`, `2000000000009`) — sem qualquer indicação de despacho.

---

## 3. Matriz de consultas por ID (`aliexpress.ds.product.get`)

**Não executada em live neste ambiente** — e o motivo é auditável, não
falta de tentativa:

- o ambiente não possui `ALIEXPRESS_APP_SECRET` nem `ALIEXPRESS_ACCESS_TOKEN`;
- a sessão do painel exige OAuth do admin via cookie criptografado de TTL
  curto (nunca persistido); concluir o fluxo exigiria credenciais + browser,
  fora do alcance desta entrega;
- **mesmo com credenciais, o schema oficial (§1) não contém campo de
  despacho** — a consulta retornaria produto/SKUs/estoque/preço, mas
  "origem comprovável" seria igualmente impossível por esse método.

| Candidato | Produto acessível | Variante acessível | Origem comprovável | Diferença vitrine × API |
| --- | --- | --- | --- | --- |
| 1 (`...1750`) | não executável aqui (sem credencial) | idem | **não** — campo inexistente no schema oficial | a vitrine tem `shipFrom` (site); a API DS não expõe |
| 2 (`...3897`) | idem | idem | **não** — idem | idem |
| 3 (`...4256`) | idem | idem | **não** — idem | idem |
| Controle (`2000000000001`) | sim, via Fake (testes) | sim | n/a (sintético, sem despacho) | — |

Nenhuma chamada de compra/submissão foi feita. Nenhum endpoint novo foi
tentado (FREIGHT_PROBE.md continua encerrado).

---

## 4. Mudanças implementadas e limitações

Como não há filtro oficial de despacho, o suporte comprovado entregue é a
**consulta direta por ID/URL usando o caminho existente de detalhe**,
mais a honestidade na interface:

- `parse_aliexpress_product_ref()` (`product.py`, exportado no pacote):
  aceita ID numérico (8–20 dígitos) ou URL `/item/<id>.html` **somente**
  em hosts `*.aliexpress.com` / `*.aliexpress.us`; recusa qualquer outro
  host, caminho, esquema ou formato — nenhuma requisição arbitrária.
- `POST /buscar-id` (`aliexpress_admin_routes.py`): valida, chama
  `client.get_product()` (read-only existente) e reaproveita a renderização
  do `detalhe` (refatorado para `_render_detalhe`); ID inválido/ausente e
  erro de API viram flash sanitizado, sem exceção e sem vazar `raw`.
- `catalogo.html`: segundo formulário "Consultar anúncio direto por ID ou
  URL" + nota explícita de que a busca do fornecedor não filtra por país
  de despacho e que a origem deve ser conferida variante a variante.
- O filtro `?origem=` dos importados **não** foi renomeado nem
  reapresentado como busca nacional.

**Limitações assumidas:** sem filtro nacional no catálogo do fornecedor
(inexistente na API oficial); sem prova de despacho por variante via API;
importação de candidato real não testada em live (sem credencial) — o fluxo
de importação foi validado com detalhe sintético (§5/Testes). Nenhum
rascunho foi publicado; frete/impostos/custo/margem seguem obrigatórios e
nenhum valor ausente é preenchido com zero. Operação internacional
intocada; confirmação nacional continua por variante explícita.

---

## 5. Diagnóstico e próximos caminhos

Diferenciação dos candidatos não acessíveis (§3): **falha de configuração
para este ambiente** (sem `APP_SECRET`/token; sessão OAuth indisponível)
**combinada com limitação do catálogo do método** (schema oficial sem
campo de despacho — com credencial, o produto e a variante seriam
acessíveis, mas a origem continuaria não comprovável por esse método).

Métodos oficiais avaliados antes de propor mudança: `ds.text.search` (sem
filtro de despacho), `ds.product.get` (sem campo de despacho),
`ds.image.search` (`shpt_to` = destino), fretes DS/buyer (FREIGHT_PROBE
encerrado: provider indisponível para a App Key). **Nenhuma migração da
integração é proposta.**

Próximos caminhos possíveis (fora desta entrega): concluir o OAuth do admin
e repetir a matriz do §3 em live; instrumentar a observação de
`ship_from_country` quando o frete voltar a responder (etapa já prevista na
Etapa 1); avaliar, com conta e permissões adequadas, se algum método DS
mais recente expõe despacho — sem adotar nada por nome sugestivo.

## 6. Testes executados, resultados e skips

Novo: `tests/test_aliexpress_lookup_by_id.py` — **13 passed**.
Cobre: parser (IDs/URLs válidos e recusas, incl. host malicioso
`aliexpress.com.evil.com`), rota (sucesso por ID e por URL, inválido sem
chamar a API, ausente, erro sanitizado sem vazar `raw`), busca geral sem
parâmetros de despacho, importação idempotente do candidato sintético
(identidade, SKUs, estoque, preço, moeda, `sku_property_id`/
`property_value_id`), origem `unknown` pós-importação e confirmação de uma
variante resultando em `mixed` (nunca `brazil` global).

Suítes pertinentes: `test_aliexpress_admin_catalog.py` +
`test_aliexpress_client.py` + `test_aliexpress_product.py` +
`test_aliexpress_lookup_by_id.py` = **153 passed**;
`test_aliexpress_search.py` + `test_draft_import.py` +
`test_published_sync_resilience.py` = **80 passed**.
Sem skips novos; nenhum teste PG (sem alteração de persistência, consultas
ou migrações). `git diff --check` limpo.
