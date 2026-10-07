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
- H3. `remove_personal_benefit=true` *pode* remover benefícios
  personalizados/de primeira compra dos preços (retornando valores
  recorrentes) — coerente com F10 e com o caso R$ 14,23 × R$ 34,80, mas sem
  confirmação oficial e sem teste live. Registrado como **experimental**:
  sem respaldo na doc oficial consultada, **não usado como garantia** de
  exclusão de primeira compra, e o comportamento atual (`true` por padrão)
  fica **preservado** nesta rodada.

### Verificações pendentes

- P1. Repetir buscas gerais em live e medir a presença de despacho BR nos
  resultados (testa H1).
- P2. Confirmar na documentação oficial a lista de parâmetros de
  `ds.text.search` (cobrir H2).
- P3. Consultar os candidatos por ID com sessão administrativa: **parcial**
  — acessibilidade por ID confirmada para os 4 (§3). Resta a
  correspondência dos `sku_id`s (**P5**, pendente de resposta estruturada
  autenticada — sem deduzir por posição na lista ou só pelo título). O
  runner `scripts/query_aliexpress_candidates.py` repete essas 4 consultas
  read-only com saída sanitizada.
- P4. Evidência de despacho por variante: **parcial** — relato do usuário
  para o piloto cobre só CÉU AZUL / M (§3); P e G seguem sem evidência.
- P6. Custo recorrente de aquisição do piloto: **pendente** — exige
  conferência no checkout da conta de compra (variante exata, 1 unidade,
  CEP real), sem concluir a compra; ver checklist no §3.
- P7. Comparação `remove_personal_benefit` on/off (testa H3): **pendente**
  de sessão — `scripts/query_aliexpress_candidates.py --compare-benefit
  [--repeat N]` consulta `1005010478234256` e `1005010502074833` com a flag
  ligada e desligada (mercado fixo BR/BRL/pt), repetindo em sequência
  alternada. Vocabulário exato da classificação: iguais → "nenhuma
  diferença observada; efeito inconclusivo"; diferentes → "diferença
  observada, ainda não atribuída ao parâmetro".

### Pendências por anúncio e variante

| Anúncio / variante | Detail OK | SKU exato (P5) | Despacho BR | Custo recorrente (P6) | Moeda raw | Origem no banco |
| --- | --- | --- | --- | --- | --- | --- |
| `1005010478234256` CÉU AZUL / M | sim [relato] | pendente | relato p/ M; P/G sem nada | pendente | — | `unknown` |
| `1005010478234256` P, G | sim [relato] | pendente | sem evidência | — | — | `unknown` |
| `1005010502074833` CÉU AZUL / M | sim [relato] | pendente | relato ("Do Brasil"); qtd/impostos s/ confirmação | pendente (R$ 52 não comprovado) | — | `unknown` |
| `1005010660869000` A08White / 36 | sim [relato] | pendente | página "Compra internacional": **nada deduzido** | pendente (qtd p/ mapear 125,36) | pendente (`currency_code` SKU+base) | `unknown` |
| `1005010660869000` demais variantes | não investigadas | — | — | — | — | `unknown` |
| `1005013003211750`, `1005012926123897` (todas) | sim [relato] | pendente | sem evidência | — | — | `unknown` |
| `1005009519310498` (controle) | sim [relato] | n/a | n/a (internacional) | n/a | n/a | `unknown` |

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

**Fato (2026-10-06, via painel — detalhe read-only, sem importar):**
consulta de detalhe **bem-sucedida para os quatro produtos** (fonte:
`info.txt` fornecido pelo operador; conteúdo transcrito abaixo, arquivo
mantido fora do versionamento). Nenhum erro retornado em nenhuma consulta.

| Candidato | Produto retornado | SKUs / variações | Origem comprovável | Erro (sanitizado) |
| --- | --- | --- | --- | --- |
| 1 (`1005013003211750`) | sim — "Vestido Feminino 2026 com Estampa Floral, Alças Finas, Estilo Boho", categoria `200000347` | 20 variações, preço uniforme R$ 69,85; estoques 9999 na maioria (0/122/130 em algumas) | pendente — resposta sem campo de despacho | nenhum |
| 2 (`1005012926123897`) | sim — "Vestidos de Verão Femininos, Vestido Maxi Floral", categoria `200000347` | 16 variações; R$ 62,50 (maioria) e R$ 59,50 (Verde P/M/G, Verde GG); estoques 119–2000 | pendente — idem | nenhum |
| 3 / piloto (`1005010478234256`) | sim — "Vestido Sem Manga em Algodão", categoria `200000347` | 3 variações (CÉU AZUL P/M/G), preço uniforme R$ 34,80, estoque 1111 em cada | pendente — idem | nenhum |
| Controle (`1005009519310498`) | sim — "Outono feminino agasalho com zíper e calças", categoria `201530602` | 12 variações, preço uniforme R$ 312,99; estoques 217–433 | n/a (controle internacional) | nenhum |

**Conclusão parcial (fato):** os três candidatos são acessíveis por ID via
`ds.product.get` — detalhe completo (título, categoria, variações, estoque,
preço) retornado para todos.
**Pendentes:** (a) despacho nacional por variante — a resposta não traz o
campo e o TXT não registra despacho; (b) correspondência dos IDs de SKU —
o TXT lista atributos/estoque/preço, mas não os `sku_id`s, então não há
como amarrar cada linha ao identificador da API.

### Piloto de validação: `1005010478234256`

Escolhido por ter a menor superfície: 3 variações (CÉU AZUL P/M/G), preço
uniforme R$ 34,80 e estoque 1111 em cada — qualquer divergência futura
(estoque, preço, SKU ausente) é imediatamente visível.

### Evidência fornecida pelo usuário (relato de tela, 06/10/2026)

**Fonte:** relato do usuário sobre a tela do AliExpress — **não** é captura
arquivada nem verificação independente. Alcance: **somente CÉU AZUL / M**;
nada se estende a P ou G.

| Campo | Evidência fornecida |
| --- | --- |
| Produto | `1005010478234256` |
| Variante | CÉU AZUL / M |
| País de despacho exibido | Brasil |
| CEP consultado | `77066520` |
| Frete exibido | Grátis |
| Método | Entrega Padrão 3PL — J&T-Commercial |
| Entrega estimada | 13/10/2026 (observação do momento, não prazo fixo da loja) |
| Data do relato | 06/10/2026 |
| Preço promocional | R$ 14,23 — oferta de primeira compra (não é custo recorrente) |
| Preço original informado | R$ 58,00 (exibido; não prova o valor cobrado) |
| Preço retornado pela API | R$ 34,80 (observado; a confrontar com o cenário de aquisição) |
| Quantidade | **pendente** — sem confirmação explícita no relato |
| Impostos | **pendente** — sem confirmação explícita no relato |

### Identificação do SKU exato (pendente — P5)

Sem sessão autenticada neste ambiente, a correspondência de CÉU AZUL / M
ao `sku_id` externo, `sku_property_id`, atributos, preço/moeda/estoque e
data/hora da consulta **não foi feita**. Não deduzir por posição na lista
nem só pelo título; o `skuId` do cartão da vitrine é formato de storefront,
não o `sku_id` da API DS. Enquanto houver ambiguidade, **nada de origem é
confirmado no banco**.

### Custo recorrente (pendente — P6)

Quatro conceitos distintos — **nenhum substitui automaticamente o custo
cadastrado**, e os dois "originais" abaixo **não** são custos recorrentes
comprovados:
- preço da API = `offer_sale_price` com `remove_personal_benefit=true`
  (piloto: R$ 34,80) → a confrontar com o cenário de aquisição (preço
  observado ≠ custo total validado);
- oferta pessoal = benefício de primeira compra/Welcome Deal, só na conta
  elegível (piloto: R$ 14,23) → não serve como custo recorrente;
- preço de referência = original exibido no anúncio (piloto: R$ 58,00;
  produto `1005010502074833`: R$ 52,00, preço original do cartão) → não
  prova o valor cobrado;
- total de checkout = subtotal + descontos + frete + impostos na conta de
  compra, variante exata, 1 unidade, CEP `77066520`, sem concluir.
Checklist pendente, na conta de compra: registrar subtotal sem benefício
de primeira compra, descontos e condições, frete, impostos/outras
cobranças, total final, despacho e prazo dessa mesma consulta. Se a conta
exibir só a oferta de primeira compra, o custo recorrente **não pôde ser
validado** — sem simular valor removendo descontos por cálculo.

### Segundo produto e comparação on/off (pendente — P7)

`1005010502074833` (cartão da vitrine: vestido, preço original R$ 52,00;
variante CÉU AZUL / M a confirmar na resposta) entra na comparação com o
piloto: ambos consultados com `remove_personal_benefit` ligado e desligado
(mercado fixo BR/BRL/pt, repetições em sequência alternada via `--repeat`),
registrando por chamada ID bruto, `sku_attr`, preço, moeda, estoque e
data/hora. Classificação em vocabulário exato: iguais → "nenhuma diferença
observada; efeito inconclusivo"; diferentes → "diferença observada, ainda
não atribuída ao parâmetro" (testa H3). Sem sessão, as chamadas estão
preparadas em `scripts/query_aliexpress_candidates.py --compare-benefit`
(testes offline verdes); custos cadastrados e origem atual preservados —
nada importado, publicado ou comprado. Comportamento atual preservado
(`true` por padrão em `get_product()` e nas rotas); eventual remoção ou
mudança de padrão é proposta separada, com avaliação dos consumidores do
preço (importação/custo, precificação, divergência de sync, exibição).

### Mecanismo de credenciais do runner e execução pendente

**Verificação (fato):** o runner usa `create_client_from_env(require_token=True)` —
`ALIEXPRESS_APP_KEY` + `ALIEXPRESS_APP_SECRET` + `ALIEXPRESS_ACCESS_TOKEN`
via ambiente, sem imprimir, copiar ou persistir segredos (saída só com
resumo/erro sanitizado). É o mesmo padrão seguro dos smoke scripts do repo.
Reuso do `credential_store` (tokens criptografados no banco por supplier)
foi avaliado e **rejeitado**: exigiria ler o banco de produção + a chave
`ALIEXPRESS_CREDENTIAL_KEY` no ambiente — superfície maior, sem reduzir o
que precisa ser fornecido. Nenhum token em documentos (só nomes).

**Execução pendente (sem credenciais neste ambiente — verificado por
presença, sem ler valores):** shell com as 3 envs exportadas, repo + venv +
rede para `api-sg.aliexpress.com`, e então, sem ecoar segredos:
`PYTHONPATH=. python scripts/query_aliexpress_candidates.py --compare-benefit --repeat 3`.
Identificação inequívoca de CÉU AZUL / M = `sku_id` + `sku_attr` completo +
atributos com IDs (rótulos "CÉU AZUL"/"M" sozinhos não bastam); a tabela
abaixo é o molde sanitizado a preencher.

| Produto | Flag | Rodada | SKU CÉU AZUL / M (`sku_id`) | `sku_attr` | Preço (BRL) | Estoque | Horário | Veredito |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `1005010478234256` | on/off ×3 | 0–2 | pendente | pendente | pendente | pendente | pendente | pendente |
| `1005010502074833` | on/off ×3 | 0–2 | pendente | pendente | pendente | pendente | pendente | pendente |

Erros, se houver, vão em registros próprios (`type`/`code`/`sub_code`/
`message`, sem `raw`) e a rodada correspondente classifica como
"comparação inviável"; diferenças nunca são atribuídas ao parâmetro sem
evidência adicional.

### Passos para executar nos dois anúncios (somente leitura)

No catálogo autenticado (sessão AliExpress válida), para cada ID
(`1005010478234256`, `1005010502074833`): abrir o detalhe (ou consultar
por ID/URL), clicar **Rodada 1**, **Rodada 2** e **Rodada 3** no card
"Comparar preço com/sem parâmetro experimental", e registrar por rodada o
veredito e os preços on/off por variante — sem importar, publicar ou
comprar. Regra de leitura: qualquer rodada com diferença → "diferença
observada, ainda não atribuída ao parâmetro"; as 3 iguais → "nenhuma
diferença observada; efeito inconclusivo".

### Preparação do cadastro do piloto (não executada)

Procedimento preparado com os fluxos existentes, para revisão — **nenhuma
importação ou alteração executada**:
1. importar como rascunho pelo fluxo existente, reaproveitando eventual
   vínculo (sem publicar);
2. confirmar despacho BR **exclusivamente** para o SKU de CÉU AZUL / M após
   P5, com a evidência acima como referência;
3. manter P e G como `unknown` até verificação própria;
4. cadastrar cotação de frete **zero** pelo fluxo manual existente somente
   para SKU/CEP/quantidade validados (quantidade 1 assumida do padrão,
   **a confirmar** — evidência sem confirmação explícita), com data e
   validade exigidas; sem generalizar para outros CEPs/quantidades; prazo
   13/10 tratado como observação, não SLA;
5. impostos: **não registrar zero** — ausência de informação não equivale a
   zero (bloqueia precificação corretamente até haver comprovação).
Produtos internacionais existentes: nada a excluir ou reimportar.

### Verificação de despacho por variante (parcial)

Coberta para CÉU AZUL / M do piloto via relato do usuário (tabela acima).
Restam: P e G do piloto, evidência formal arquivada, e a correspondência
do `sku_id` (P5). **Sem comprovação, a origem permanece `unknown`.**

### Novo anúncio: `1005010660869000` (tênis) — evidência do usuário, 06/10/2026

**Fonte:** conteúdo fornecido pelo usuário — **não** é captura arquivada
nem verificação independente. A página foi relatada como "Compra
internacional": **nenhum país de despacho deduzido, nenhuma origem
confirmada no banco**.

| Campo | Evidência fornecida |
| --- | --- |
| Anúncio | `1005010660869000` |
| Variante | A08White / 36 |
| ID bruto do SKU | `14:193#A08White;200000124:200000334` |
| sku_attr | `14:193;200000124:200000334` |
| Estoque informado pela API | 1099998 |
| Preço exibido no painel | 76,99 |
| Moeda exibida no painel | Ausente |
| CEP do checkout | `77066520` |
| Subtotal | R$ 76,99 |
| Frete | R$ 27,03 |
| Imposto de importação | R$ 0,00 |
| ICMS | R$ 21,34 |
| Total | R$ 125,36 |

**Conclusão estrita:** mesmo anúncio e variante, coincidência numérica
entre preço da API (76,99) e subtotal do checkout (R$ 76,99); moeda da API
ainda não esclarecida. Quantidade de uma unidade **pendente de
confirmação explícita**. Observação: o `id` bruto confirma o formato
combinação com fragmento (`pid:vid#Rótulo;...`), como previsto na
auditoria de identidade.

### Moeda ausente — causa e correção

Rastreio `ds.product.get` → normalização → `_detalhe_sku_rows` → template
**[código, verificado por testes com fixtures]**: a resposta oficial traz
`currency_code` no SKU e na base, e o código preserva os dois
(`sku.currency`, `detail.currency`); a linha usa o da variante com
fallback para o do produto. Nada é descartado nesse caminho.
**[dedução, não fato observado]**: se a tela mostra ausência, a hipótese é
ausência na resposta live — **pendente** dos `currency_code` brutos (SKU e
base) de `1005010660869000` para fechar. Correção aplicada: sem moeda
retornada, a tela mostra **"Não fornecida"** (células e linha "Moeda:"), e
o BRL do destino nunca é usado como fallback silencioso (coberto por
teste). Nova ação `POST /<id>/moeda-bruta` (admin+sessão+CSRF, só leitura,
sem JSON completo) exibe exatamente esses dois campos para o caso concreto.

### Auditoria do custo completo de aquisição (sem gravar valores)

Consumidores do preço verificados: importação (custo base do maior SKU),
precificação (`landed = cost + shipping + import_duty + icms`, fail-closed
por `observed_amount`; margem comercial aplicada **depois**, sobre o
landed), sincronização (sinaliza divergência, nunca aplica), margem
(`target_margin_percent` explícito, 0–100). Para o checkout informado:
76,99 + 27,03 + 0,00 + 21,34 = 125,36 — aritmética confere, mas **R$ 76,99
não é custo completo** (falta frete + tributos), e os tributos só entram
uma vez (sem soma dupla no código). Moeda única exigida (sem camada FX).
**Lacunas:** (a) mapeamento desses valores para os campos por unidade
(`cost`, `shipping_cost`, `import_duty`, `icms`) exige quantidade 1
confirmada — pendente; (b) docstring de `calculate_landed_cost` dizia só
"cost + shipping" — **corrigida nesta rodada** (inclui os quatro
componentes; sem impacto funcional). **Nenhum valor do relato foi gravado
como cotação ou custo validado de produção.**

### Consolidação desta rodada (fontes marcadas)

- `1005010478234256`, CÉU AZUL/M **[relato]**: API R$ 34,80; outra conta
  apresentou R$ 58,00.
- `1005010502074833`, CÉU AZUL/M **[relato]**: API R$ 31,20; outra conta
  apresentou R$ 52,00.
- "Do Brasil" e frete grátis **[relato]** em ambos; quantidade e impostos
  sem confirmação explícita.
- Três rodadas on/off por anúncio sem diferenças **[relato do operador,
  não reexecução local]** → "nenhuma diferença observada; efeito
  inconclusivo".
- A causa da divergência dos vestidos permanece desconhecida; o caso do
  tênis não valida todos os preços da API. Rodadas não repetidas por aqui;
  padrão de `remove_personal_benefit` inalterado **[código]**.

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
- `catalogo.html`: formulário de consulta por ID **primeira ação** do
  catálogo (antes da busca geral e da tabela de importados) com botão
  visível "Consultar anúncio" ligado só ao `POST /buscar-id`; cada campo
  está no próprio form com submit próprio (Enter não cruza formulários).
  Correção real aplicada: o botão usava `bg-gray-700`, classe ausente no
  `tailwind.css` compilado (purgada) — botão sem fundo, invisível no cartão
  branco; trocado por `bg-gray-800 hover:bg-gray-900` (presentes no build).
- `detalhe.html` (+ `_detalhe_sku_rows` / `_render_detalhe` na rota): a
  tabela de variações exibe, por variante, `sku_id` DS **só** com o campo
  `id` bruto (ausente → "Não fornecido", sem substituir), `sku_attr`
  canônico `pid:vid` construído pelos IDs dos atributos (mesma função do
  envio de pedidos), pares `Nome (pid) = Valor (vid)`, cor, tamanho, preço,
  moeda e estoque — montados da resposta já consultada (nenhuma chamada
  adicional, nenhuma persistência), sob os mesmos `@admin_required` +
  `@ali_session_required`; ID do produto junto ao título da tabela.
- `POST /<id>/comparar-preco` (+ card "Rodada 1/2/3" no `detalhe.html` e
  página `comparar.html`): **uma rodada por acionamento** (2 consultas
  sequenciais, ≤20s no pior caso — abaixo do teto de 60s da função;
  6 chamadas de uma vez arriscariam timeout). Reutiliza cliente e sessão
  da rota de detalhe (sem credential-store); exige admin, sessão válida e
  CSRF; mostra por variante ID do anúncio, SKU bruto, `sku_attr` on/off,
  atributos, preços e estoques on/off, moeda, veredito da rodada e erros
  sanitizados; parâmetro marcado experimental; **zero acesso ao banco**.
- `POST /<id>/moeda-bruta` (+ card no `detalhe.html` e página `moeda.html`):
  exibe só `currency_code` da base e da variante exata (`sku_id`
  opcional; ausente lista todas), sem JSON completo e sem credenciais;
  admin+sessão+CSRF, só leitura, zero banco.
- Auditoria de fallback de identidade (sem alterar registros): importação
  pula SKU sem id e grava o `id` verbatim (igualdade de strings no sync e
  no enrichment; nenhum `int()` em IDs em todo o módulo); fulfillment exige
  `supplier_sku_id` presente mas envia `sku_attr` canônico dos atributos
  (fail-closed sem ele); frete repassa o id bruto. Único fallback por
  derivação está em `buyer_freight_get.canonical_freight_sku_id` (caminho de
  frete com provider bloqueado, sem efeito em produção) — documentado, não
  alterado.
- `catalogo.html`: sem sessão AliExpress possível (env ausente), a página
  explicava nada — cabeçalho seguido direto da tabela de importados. Agora
  exibe o aviso "Integração AliExpress não configurada neste ambiente".
  Divergência código × Preview investigada via Deployments API: o alias da
  branch aponta para o deployment `success` mais recente (98d6ee9); sem
  sessão, os formulários ficam ocultos por construção (`{% if connected %}`),
  pois exigem o token — comportamento preservado.
- `summarize_product_detail()` / `format_candidate_error()` (`product.py`):
  registro sanitizado para a matriz live (sem `raw`/segredos).
- O filtro `?origem=` dos importados **não** foi renomeado nem
  reapresentado como busca nacional.

**Limites desta entrega (fatos):** sem filtro de despacho na busca oficial
(F1–F7); sem prova de despacho por variante via API (F5) — a origem segue
por evidência externa + confirmação manual por variante (fluxo da Etapa 1,
inalterado); importação de candidato real não testada em live (P3) — o
fluxo de importação foi validado com detalhe sintético (§6); cadastro do
piloto **preparado, não executado** (P5/P6 pendentes). Nenhum rascunho foi
publicado; frete/impostos/custo/margem seguem obrigatórios e nenhum valor
ausente é preenchido com zero. Operação internacional intocada;
confirmação nacional continua por variante explícita.

---

## 5. Diagnóstico e próximos caminhos

Candidatos não acessíveis em live: **fato** — F8 (sem `APP_SECRET`/token
neste ambiente; OAuth indisponível aqui). A acessibilidade por ID foi
confirmada via painel pelo operador (§3): os 4 produtos retornam detalhe
completo, sem erros. Seguem pendentes a correspondência dos `sku_id`s e a
evidência de despacho por variante (protocolo no §3; origem `unknown` até
lá).

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

Novo: `tests/test_aliexpress_lookup_by_id.py` — **52 passed**.
Cobre: parser (IDs/URLs válidos e recusas, incl. host malicioso
`aliexpress.com.evil.com`), rota (sucesso por ID e por URL, inválido sem
chamar a API, ausente, erro sanitizado sem vazar `raw`, nota de limitação
na UI), resumo sanitizado e erro sanitizado, busca geral sem parâmetros de
despacho, tabela de detalhe com IDs e dimensões por variante
(correspondência atributo↔ID por linha, caso sem IDs, login admin e sessão
AliExpress exigidos), importação idempotente do candidato sintético
(identidade, SKUs, estoque, preço, moeda, `sku_property_id`/
`property_value_id`), origem `unknown` pós-importação e confirmação de uma
variante resultando em `mixed` (nunca `brazil` global), aviso explícito
quando a integração não está configurada (sem formulários fantasmas),
piloto `1005010478234256` abrindo o detalhe exato via `/buscar-id`,
exclusividade/estrutura/visibilidade-CSS do botão "Consultar anúncio",
separação `sku_id` × `sku_attr` × IDs (ausente → "Não fornecido"),
runner `--compare-benefit` com flag, rodada e mercado registrados por
chamada (erro isolado por flag) e classificação em vocabulário exato
(iguais → efeito inconclusivo; diferentes → ainda não atribuída), ação
`comparar-preco` com alternância por paridade, falhas parciais
sanitizadas, rodada inválida e ausência total de escritas no banco,
moeda por nível (variante, produto, ausente → "Não fornecida" sem
fabricar o BRL do destino), ação `moeda-bruta` (auth/sessão/405,
filtro por SKU exato, sanitização do raw, sem escritas).

Suítes pertinentes: `test_aliexpress_admin_catalog.py` +
`test_aliexpress_client.py` + `test_aliexpress_product.py` +
`test_aliexpress_lookup_by_id.py` = **156 passed**;
`test_aliexpress_search.py` + `test_draft_import.py` +
`test_published_sync_resilience.py` = **80 passed**.
Suíte completa: **2233 passed, 107 skipped**.
Sem skips novos; nenhum teste PG (sem alteração de persistência, consultas
ou migrações). `git diff --check` limpo.
