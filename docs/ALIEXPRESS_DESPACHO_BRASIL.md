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

### Referências oficiais (conferidas em 2026-10-06)

- API Reference (índice oficial): <https://openservice.aliexpress.com/doc/api.htm>
- `aliexpress.ds.product.get` (request + resposta completos):
  <https://developer.alibaba.com/docs/api.htm?apiId=60452>
- `aliexpress.ds.image.search` (`shpt_to` = destino):
  <https://developer.alibaba.com/docs/api.htm?apiId=60453>
- `aliexpress.ds.text.search`: sem página oficial recuperada com a lista de
  parâmetros; o conjunto usado (`keyWord`, `local`, `countryCode`,
  `categoryId`, `sortBy`, `pageIndex`, `pageSize`, `currency`,
  `searchExtend`, `selectionName`) é corroborado pelo código live do repo,
  pelos SDKs TOP e por integrações DS de terceiros — ver hipótese H2.

### Fatos (verificados)

- F1. A busca envia exatamente os parâmetros da tabela acima; `categoryId`,
  `searchExtend` e `selectionName` nunca são enviados pelo painel.
- F2. O painel expõe um único input de busca (`q`); não há filtro de
  despacho, ordenação, categoria ou paginação na UI.
- F3. O filtro `?origem=` atua só sobre produtos já importados (banco
  local); não é busca nacional no fornecedor.
- F4. `countryCode=BR` (busca) e `ship_to_country=BR` (detalhe) são país do
  comprador/destino — não provam nem filtram despacho brasileiro.
- F5. O schema oficial de `ds.product.get` não contém nenhum campo de país
  de despacho (resposta cobre base, SKUs, multimídia, pacote, logística de
  destino, propriedades, loja e conversor de IDs).
- F6. `searchExtend`/`selectionName` não têm semântica documentada
  localizada; não foram adotados.
- F7. O `shipFromCountry`/`shipFrom` existe na busca pública do site
  (filtro de vitrine), não na Open Platform DS.
- F8. Neste ambiente não há `ALIEXPRESS_APP_SECRET` nem
  `ALIEXPRESS_ACCESS_TOKEN`; a sessão do painel exige OAuth do admin via
  cookie criptografado de TTL curto (nunca persistido).

### Hipóteses (não testadas — não adotar como premissa de código)

- H1. A predominância de anúncios CN nos resultados pode decorrer da
  ordenação fixa `orders,desc` e/ou da composição do catálogo DS — não
  medido; nenhum parâmetro enviado restringe por despacho (F1, F4).
- H2. `searchExtend`/`selectionName` *podem* ter semântica útil de seleção
  (a resposta live traz `selection_search_product`) — sem confirmação
  oficial, seguem sem uso.

### Verificações pendentes

- P1. Repetir buscas gerais em live e medir a presença de despacho BR nos
  resultados (testa H1).
- P2. Confirmar na documentação oficial a lista de parâmetros de
  `ds.text.search` (cobrir H2).
- P3. Com sessão administrativa (ver §3), consultar os candidatos por ID e
  registrar produto/SKUs/erros — testa se o catálogo DS os contém.
- P4. Confirmar por variante, no anúncio com login, a opção de despacho BR
  (testa a evidência do §2 por variante).

---

## 2. Candidatos com despacho brasileiro (vitrine pública, somente leitura)

**Fato:** busca pública filtrada por `shipFromCountry=BR`
(`Local de envio: Brasil` ativo), 2026-10-06, sem login e sem compra —
60 cartões retornados, todos com telemetria `"shipFrom":"BR"` amarrada ao
`skuId` do cartão (SKU padrão de cada anúncio).

**Pendente (P4):** a indicação acima é em nível de anúncio/SKU padrão; o
despacho da **variante escolhida** (tamanho/cor) só aparece no anúncio com
login/JS — página inacessível a fetch sem login. Por isso **nenhum
candidato é apresentado como confirmado**.

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

**Fato:** não executada em live neste ambiente — sem `ALIEXPRESS_APP_SECRET`
nem `ALIEXPRESS_ACCESS_TOKEN` (F8); a sessão do painel exige OAuth do admin
via browser, fora do alcance desta entrega.

**Pendente (P3):** com sessão administrativa, consultar os três candidatos
e um produto internacional real, registrando produto retornado, SKUs
correspondentes e erros sanitizados — sem publicar nem comprar. O runner
`scripts/query_aliexpress_candidates.py` executa exatamente essas 4
consultas read-only (`ds.product.get` com os defaults do painel) e imprime
só o resumo sanitizado (`summarize_product_detail`) ou o erro sanitizado
(`format_candidate_error`) — nenhum segredo na saída. Só então se saberá
se o catálogo DS contém esses anúncios. A ausência de campo de despacho no
schema (F5) não impede esse teste: produto/SKUs/estoque/preço podem ser
verificados e a origem seguir confirmada manualmente por variante.

| Candidato | Produto retornado | SKUs correspondentes | Origem comprovável | Erro (sanitizado) |
| --- | --- | --- | --- | --- |
| 1 (`...1750`) | pendente (P3) | pendente (P3) | pendente — schema oficial não prevê campo (F5) | — |
| 2 (`...3897`) | pendente (P3) | pendente (P3) | pendente — idem | — |
| 3 (`...4256`) | pendente (P3) | pendente (P3) | pendente — idem | — |
| Controle internacional (`1005009519310498`, produto real citado em `docs/SUPPLIER_PURCHASE_TAXES.md`) | pendente (P3) | pendente (P3) | n/a | — |

Nenhuma chamada de compra/submissão foi feita. Nenhum endpoint novo foi
tentado (FREIGHT_PROBE.md continua encerrado). Os IDs sintéticos do repo
(`2000000000001`, `2000000000009`) seguem como controle offline nos testes.

### Caminhos validados para executar P3 (somente leitura, sem importar)

**Via Preview do PR (com o formulário novo) — validado por código e sonda
em 2026-10-06:** rotas montadas e gate/CSRF ativos (catálogo, detalhe e
`POST /buscar-id` respondem; callback branch-aware
`https://<branch>/integrations/aliexpress/callback` via Node → cookies →
`/callback/complete` com state de uso único de 10 min). Passos no browser:
abrir o catálogo do Preview, login admin, autorizar o AliExpress se pedir,
usar "Consultar anúncio direto por ID ou URL" para cada ID abaixo e
registrar a página de detalhe. **Não clicar em "Importar como rascunho".**
Ressalva honesta: `APP_KEY`/`SECRET` no env do Preview e o callback do
Preview registrado no console do app AliExpress não são verificáveis daqui
— se o AliExpress recusar o `redirect_uri`, usar o fallback.

**Fallback em produção (fluxo existente, já aceita qualquer ID no path):**
com admin + sessão AliExpress ativos, abrir cada URL e registrar título,
SKUs/variações, preços e estoque — sem importar:

- `https://www.pollomodas.com.br/admin/integrations/aliexpress/1005013003211750/detalhe`
- `https://www.pollomodas.com.br/admin/integrations/aliexpress/1005012926123897/detalhe`
- `https://www.pollomodas.com.br/admin/integrations/aliexpress/1005010478234256/detalhe`
- `https://www.pollomodas.com.br/admin/integrations/aliexpress/1005009519310498/detalhe` (controle)

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
  URL" + nota curta e factual: sem filtro de despacho na busca, consultar
  por ID/URL e conferir por variante, BR só com evidência por variante.
- `summarize_product_detail()` / `format_candidate_error()` (`product.py`):
  registro sanitizado para a matriz live (sem `raw`/segredos).
- O filtro `?origem=` dos importados **não** foi renomeado nem
  reapresentado como busca nacional.

**Limites desta entrega (fatos):** sem filtro de despacho na busca oficial
(F1–F7); sem prova de despacho por variante via API (F5) — a origem segue
por evidência externa + confirmação manual por variante (fluxo da Etapa 1,
inalterado); importação de candidato real não testada em live (P3) — o
fluxo de importação foi validado com detalhe sintético (§6). Nenhum
rascunho foi publicado; frete/impostos/custo/margem seguem obrigatórios e
nenhum valor ausente é preenchido com zero. Operação internacional
intocada; confirmação nacional continua por variante explícita.

---

## 5. Diagnóstico e próximos caminhos

Candidatos não acessíveis em live: **fato** — F8 (sem `APP_SECRET`/token;
OAuth indisponível neste ambiente). Nada se afirma sobre a presença deles
no catálogo DS: é a verificação **P3**, pendente de sessão administrativa.

Métodos oficiais avaliados antes de propor mudança: `ds.text.search`
(parâmetros auditados, sem filtro de despacho), `ds.product.get` (schema
oficial completo, sem campo de despacho), `ds.image.search` (`shpt_to` =
destino), fretes DS/buyer (FREIGHT_PROBE encerrado: provider indisponível
para a App Key). **Nenhuma migração da integração é proposta.**

Próximos caminhos (fora desta entrega): P1–P4 acima; instrumentar a
observação de `ship_from_country` quando o frete voltar a responder (etapa
já prevista na Etapa 1); avaliar, com conta e permissões adequadas, se
algum método DS mais recente expõe despacho — sem adotar nada por nome
sugestivo.

## 6. Testes executados, resultados e skips

Novo: `tests/test_aliexpress_lookup_by_id.py` — **16 passed**.
Cobre: parser (IDs/URLs válidos e recusas, incl. host malicioso
`aliexpress.com.evil.com`), rota (sucesso por ID e por URL, inválido sem
chamar a API, ausente, erro sanitizado sem vazar `raw`, nota de limitação
na UI), resumo sanitizado e erro sanitizado, busca geral sem parâmetros de
despacho, importação idempotente do candidato sintético (identidade, SKUs,
estoque, preço, moeda, `sku_property_id`/`property_value_id`), origem
`unknown` pós-importação e confirmação de uma variante resultando em
`mixed` (nunca `brazil` global).

Suítes pertinentes: `test_aliexpress_admin_catalog.py` +
`test_aliexpress_client.py` + `test_aliexpress_product.py` +
`test_aliexpress_lookup_by_id.py` = **153 passed**;
`test_aliexpress_search.py` + `test_draft_import.py` +
`test_published_sync_resilience.py` = **80 passed**.
Sem skips novos; nenhum teste PG (sem alteração de persistência, consultas
ou migrações). `git diff --check` limpo.
