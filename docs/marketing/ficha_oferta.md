# Ficha de oferta (T02) — modelo preenchível

> Instruções: copiar este modelo por oferta. Todo campo sem evidência atual
> fica explicitamente `DESCONHECIDO` — nunca preencher com preços, prazos ou
> custos antigos como se fossem atuais. Aquisição separada dos demais custos
> para não descontar duas vezes (ver § Margem).

## Identificação

| Campo | Valor | Evidência (data/fonte) |
|---|---|---|
| `offer_id` | DESCONHECIDO | — |
| `product_id` | DESCONHECIDO | — |
| `variant_id` | DESCONHECIDO | — |
| URL da oferta | DESCONHECIDO | — |
| Status (`rascunho/aprovada/descartada`) | rascunho | — |
| Responsável | DESCONHECIDO | — |

## Origem e fornecedor

| Campo | Valor | Evidência (data/fonte) |
|---|---|---|
| Origem real de despacho | DESCONHECIDO | — |
| Fornecedor | DESCONHECIDO | — |
| Condição de pagamento ao fornecedor | DESCONHECIDO | — |

## Custos do produto (sem aquisição)

| Campo | Valor (BRL) | Evidência (data/fonte) |
|---|---|---|
| Custo do produto | DESCONHECIDO | — |
| Frete do fornecedor | DESCONHECIDO | — |
| Encargos não incluídos | DESCONHECIDO | — |
| Taxas de pagamento (observado, não estimado) | DESCONHECIDO | — |
| Tributos aplicáveis | DESCONHECIDO | — |
| Outros custos variáveis | DESCONHECIDO | — |
| Provisões definidas | DESCONHECIDO | — |

## Aquisição (separada — não somar acima)

| Campo | Valor | Evidência (data/fonte) |
|---|---|---|
| Canal | DESCONHECIDO | — |
| CAC máximo autorizado | DESCONHECIDO | — |
| Teto de gasto do teste | DESCONHECIDO | — |
| Limite de perda por hipótese | DESCONHECIDO | — |

## Venda e prazo

| Campo | Valor | Evidência (data/fonte) |
|---|---|---|
| Preço de venda | DESCONHECIDO | — |
| Desconto aplicado | DESCONHECIDO | — |
| Frete cobrado | DESCONHECIDO | — |
| Prazo por destino (com data da consulta) | DESCONHECIDO | — |
| Validade desta consulta | DESCONHECIDO | — |

## Amostra

| Campo | Valor | Evidência (data/fonte) |
|---|---|---|
| Status da amostra | DESCONHECIDO | — |
| Divergências vs anúncio | DESCONHECIDO | — |

## Margem (fórmulas — calcular com números evidenciados)

```text
receita            = produtos + frete cobrado − descontos
custos_variaveis   = produto + fretes pagos + encargos + taxas + tributos
                     + outros variáveis + provisões   (SEM aquisição aqui)
contribuicao_antes = receita − custos_variaveis
cac_maximo         = contribuicao_antes − contribuicao_desejada
```

Regras: nunca somar imposto já incluído no custo; aquisição entra só no
`cac_maximo`, nunca em `custos_variaveis`; campo desconhecido permanece
`DESCONHECIDO` e bloqueia a aprovação da oferta.
