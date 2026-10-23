## Escopo

Implementação das **Etapas 1–3** da integração AliExpress em uma única branch.

- **Nenhuma** compra automática, pedido real, cron, despublicação ou alteração de preço/margem foi criada.
- Nenhum merge/deploy sem aprovação. As mudanças visíveis (painel de importados, selos de origem, botões de origem/nacional/sincronizar) também dependem de aprovação.
- Documentação completa: [`docs/ALIEXPRESS_ORIGEM_ENVIO.md`](docs/ALIEXPRESS_ORIGEM_ENVIO.md).

Commits (3 por etapa + documentação/CI/correção de teste):

| Commit | Etapa |
| --- | --- |
| `0ecb9ee` | Etapa 1 — origem de envio por variante com evidência (migração 0034, serviço, gates) |
| `1aea6e0` | Etapa 2 — busca administrativa, exibição honesta e seleção de operação nacional |
| `ef0176d` | Etapa 3 — sincronização técnica de produto publicado |
| `4882687` | docs + regeneração do `tailwind.css` |
| `65a5a49` | migração 0034 incluída na suíte PostgreSQL do CI |
| `f92f220` | correção da consulta de índices (`pg_indexes`) no teste da migração |
| `54aeefb` | docs: migração 0034 registrada na suíte PG do CI |

**CI (head `54aeefb`): 4/4 checks verdes** — `pytest`, `pytest-postgres`,
`pytest-pricing-pg`, `Vercel Preview Comments`.

---

## 1. O que foi implementado, por etapa

**Etapa 1** — `migrations/0034_shipping_origin_evidence.sql` cria
`supplier_sku_shipping_origins` (colunas separadas para `manual_origin` +
`evidence_ref`/`confirmed_at`/`confirmed_by_user_id` e para `api_origin` +
`api_observed_at`), escopo XOR por linha (SKU **ou** cobertura total) com
CHECKs e índices únicos parciais, mais a coluna
`product_variants.national_shipping_operation` (`FALSE` por padrão, sem
backfill) registrada em `loja/schema.py` (`REQUIRED_COLUMNS`).
`loja/services/shipping_origin.py` faz a resolução **fail-closed**
(ausência de linha = desconhecida; precedência SKU > cobertura; API nunca
sobrescreve a confirmação manual; conflito `manual × api` sinalizado; resumo
do produto `brazil|foreign|unknown|mixed|conflict` — 1 SKU BR + 1
desconhecida nunca vira nacional). `sales_readiness` e `publication` ganham os
códigos BLOCKING estáveis `national_origin_unproven`,
`national_origin_foreign` e `national_origin_conflict`: a publicação exige
origem Brasil comprovada **por variante selecionada**, e `evaluate_publication`
**nunca despublica** o que já está à venda.

**Etapa 2** — rotas `POST /admin/integrations/aliexpress/{rascunho,publicado}/<id>/origem`
e `.../nacional` (admin + sessão AliExpress), validação antes de qualquer
escrita, `evidence_ref` obrigatória, transação única com `commit` único e
rollback em falha; limpar a confirmação preserva observações da API. Catálogo
ganha o painel **Produtos importados** (resumo em lote — uma única consulta),
filtros `?origem=brazil|foreign|unknown|mixed|conflict` (valor inválido é
ignorado) e teto de 50 itens. Resultados de busca e página de detalhe **nunca**
apresentam produto não importado como nacional: o selo é sempre *Origem não
verificada* até haver evidência gravada. Parcial compartilhado
`_origin_fields.html` em rascunho e renovação de frete.

**Etapa 3** — `loja/integrations/aliexpress/published_sync.py` + rota
`POST /publicado/<id>/sincronizar` (botão na página de renovação). Só
atualiza estoque/dados técnicos a partir do detalhe **já obtido** pela rota: o
módulo não tem credenciais e não faz chamada de frete/compra/pedido.
Divergência de custo/moeda é comparada e **sinalizada** sem aplicar.

## 2. Campos da API que comprovam origem — referência e limitações

Referência oficial: **AliExpress Open Platform — API Reference**
<https://openservice.aliexpress.com/doc/api.htm> (roteador TOP documenta os
parâmetros das queries de frete).

| Campo | Natureza | Uso aqui |
| --- | --- | --- |
| `ship_from_country` | origem do envio **cotada/pedida**; nas docs oficiais aparece também como parâmetro de requisição da query de frete | único campo de origem consumido, em `freight.py:215` (`ship_from_country` → `dispatchCountry` → `country` → `countryCode`) |
| `send_goods_country_code` | **parâmetro de requisição** de `buyer.freight.calculate` (default `CN`) | nunca é prova de origem; não é lido como evidência |
| `ship_to_country` / `countryCode` | **destino** do comprador (product.get/search) | ignorado como origem |

Limitações declaradas (por isso a origem é fail-closed):

1. O `country` da opção de frete usa *fallback* ambíguo (os últimos nomes podem
   ser destino): valor observado só entra em `api_origin` como **observação**,
   nunca como prova de Brasil.
2. O frete por resposta está **bloqueado para App Key** (null/501/505) e os
   testes do catálogo **proíbem chamadas de frete** → `record_api_observation()`
   **ainda não tem chamador em produção**; nenhuma linha `api_origin` é gravada
   hoje.
3. O payload bruto do detalhe do produto não é persistido → não dá para
   inspecioná-lo retroativamente por um campo de origem.
4. Consequência: hoje a origem só entra por **confirmação manual com
   evidência**; sem evidência o produto segue *não verificado* e a operação
   nacional selecionada fica bloqueada.

## 3. Como funciona a confirmação manual

1. Catálogo → produto importado → rascunho (ou página do publicado) →
   **Origem de envio**.
2. Escolher **escopo**: uma variante específica **ou** *Todas as variantes
   (cobertura explícita)* — XOR, nunca os dois (escopo inválido é rejeitado
   antes de gravar).
3. Escolher **Brasil** ou **Exterior**, país opcional (2 letras) e a
   **referência da evidência** (link, print, número de pedido) — sem ela nada é
   gravado.
4. Gravação em transação única; **Limpar confirmação** apaga a linha manual e
   preserva qualquer observação da API.
5. Em **Operação nacional**, marcar quais variantes ativas participam. Cada
   variante marcada precisa de origem **Brasil comprovada** para publicar; os
   bloqueios aparecem no rascunho com os códigos estáveis
   `national_origin_unproven` / `national_origin_foreign` /
   `national_origin_conflict`. Variante **não marcada** nunca bloqueia
   (catálogo internacional continua publicando normalmente).

## 4. Sincronização de publicado — atualizado × preservado

| Atualizado (técnico/estoque) | Preservado (decisão comercial) |
| --- | --- |
| `ProductVariant.stock`; desativação de variante sem estoque ou SKUs sumidos do catálogo; novos SKUs entram **inativos** | nome, descrição, `image1..4` + galeria, categoria |
| `Product.stock` (soma das variantes ativas) | `price`, `target_margin_percent`, `currency`, `cost` |
| `sync_status`/`last_sync_at`/`sync_error` (produto e vínculo) | `pricing_status`/`pricing_error` (nunca vira `stale` sozinho) |
| `ProductSync.last_synced_at`/`error` (`state` preservado) | `is_draft`/`is_active` — **nunca despublica** |
| — | frete, landed cost e evidência; origem de envio e seleção nacional; atributos/preço de variante existente; variante desativada nunca é reativada |

Divergências sinalizadas em `sync_error`: `supplier_cost_changed`,
`currency_changed`, com aviso *divergência sinalizada SEM aplicar*. Conflitos
controlados (`not_published`, `external_id_mismatch`, `link_mismatch`,
`missing_supplier_price`, `missing_currency`, `currency_mismatch`,
`variant_label_too_long`) abortam **antes** de qualquer escrita; falha na
gravação reverte tudo (rollback).

## 5. Como aplicar a migração e operar

1. Aplicar `migrations/0034_shipping_origin_evidence.sql` (reversão em
   `0034_shipping_origin_evidence_down.sql`); idempotente, schema `pollomodas`,
   cria tabela/coluna sem backfill (nenhum produto nasce nacional).
2. Nenhum passo manual extra: painel e selos aparecem sozinhos no catálogo.
3. Rotina: importar → cotar frete com evidência → precificar → confirmar origem
   por variante com evidência → marcar variantes da operação nacional →
   publicar.
4. Rotina de sync: na página do produto publicado, **Sincronizar estoque
   agora**; revisar o aviso de divergência antes de renovar frete/reprecificar.

## 6. Testes e pendências

Suíte completa local (`python -m pytest -q tests/`):
**2160 passed, 93 skipped** (2m05s). Destaques:

- `tests/test_shipping_origin_evidence.py` (39 novos: serviço, modelo, CHECKs);
- `tests/test_admin_sales_readiness.py` / `tests/test_publication.py`
  (gates de origem, incluindo "nunca despublica");
- `tests/test_aliexpress_admin_catalog.py` (rotas de origem/nacional,
  exibição honesta, filtro de importados, validação de entrada, logs sem
  evidência/token, zero chamadas de frete);
- `tests/test_published_product_sync.py` (15: preservação comercial, estoque
  zero sem despublicar, SKU novo inativo, SKU removido, divergências
  sinalizadas, idempotência, conflitos, admin/sessão).

Pendências declaradas:

1. **PostgreSQL**: `tests/test_shipping_origin_pg.py` (up/down idempotente da
   migração em schema descartável) é pulado **localmente** — não há PG local e a
   checagem de segurança `IS_SAFE_POSTGRES` impede rodar contra o banco remoto.
   Ele passou a rodar no job `pytest-postgres` do CI (lista atualizada em
   `.github/workflows/pr14-pytest.yml`, **mudança de CI que precisa de
   aprovação**) e está verde nesta execução.
2. `record_api_observation()` segue sem chamador de produção até haver
   observação real de `ship_from_country` (limitação 2 acima).
3. Novos SKUs entram inativos; ativar uma nova opção de venda continua sendo
   decisão manual (ainda não há tela para isso).
4. Aguardando aprovação — **não fazer merge**.
