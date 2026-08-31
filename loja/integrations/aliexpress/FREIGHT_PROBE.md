# Investigação live de frete — AliExpress (encerrada)

**Status: BLOQUEADO PELO PROVIDER**

A investigação live de frete (Fase 4 / 4b) está encerrada. Os caminhos de
endpoint/parâmetro testados até o momento não retornam frete utilizável para
este App Key. Não fazer novos experimentos de endpoint/parâmetro no ambiente
live.

## Implementação experimental canônica (única mantida)

É o único caminho de frete mantido em runtime no smoke (quando habilitado):

- **Método:** `aliexpress.logistics.buyer.freight.calculate`
- **Gateway:** `https://api-sg.aliexpress.com/sync`
- **Sign:** `sign_method=sha256`
- **SKU key no request:** `skuId` (camelCase)

O transporte TOP legado (`https://eco.taobao.com/router/rest`, sign `md5` via
`sign_top`) permanece apenas como módulo/teste histórico **isolado** — nunca é
escolhido em runtime para este App Key.

## Sequência validada ao vivo (somente a forma, sem valores)

| Chamada | Resultado observado |
| --- | --- |
| `buyer.freight.get` + `sku_id` | `InvalidParameter` |
| `buyer.freight.get` sem `sku_id` | `MissingParameter` |
| `buyer.freight.get` + SKU canônico | `InvalidParameter` |
| `buyer.freight.calculate` + `sku_id` | `UnsupportedParamMapping` |
| `buyer.freight.calculate` + `skuId` | request aceito até a lógica interna; `queryShippingOptionForItem(forBuyer...)` retorna `null` |
| TOP `router/rest` | `code 29` / `isv.appkey-not-exists` |

> Este documento não registra IDs, App Key, OAuth code/state, token, SKU ou
> bodies de request.

## Estado de domínio (frete indisponível)

Quando `queryShippingOptionForItem(forBuyer...)` retorna `null`, tratar como
indisponibilidade do provider — **NUNCA** como frete zero:

- `freight_status = "provider_unavailable"`
- `freight_options = []`
- `shipping_cost = None`

Consequências na precificação (a definir — ainda sem margem/importação):

- `landed_cost = None`
- `margin = None`

Produtos podem continuar sendo pesquisados e normalizados como catálogo/draft,
mas NÃO devem ficar automaticamente publicáveis enquanto o custo logístico
estiver desconhecido.

## Smoke no Preview

No Preview (`VERCEL_ENV == "preview"`) o smoke de frete fica **desativado**:
o callback não executa nenhuma chamada de frete (buyer/ds/freight.get). A
flag `ALIEXPRESS_SMOKE_BUYER_FREIGHT` continua permitindo o experimento
canônico em ambiente de homologação local, se necessário.