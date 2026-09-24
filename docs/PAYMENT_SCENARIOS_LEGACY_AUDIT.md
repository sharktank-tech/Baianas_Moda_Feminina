# Auditoria: consumidores legados de `payment_scenario` (unit economics)

Escopo desta auditoria: inventariar todo consumidor de produção de
`build_unit_economics_components` / `evaluate_unit_economics`, quantificar o
contrato aditivo legado com a configuração operacional confirmada, inventariar
a evidência de modalidade persistida por pagamento e registrar o status das
correções. O contrato legado do construtor foi mantido de propósito pelo PR #74
(ver `PAYMENT_SCENARIOS_READINESS.md`); as correções ficam nos
consumidores/adapters (branch `audit/payment-scenario-consumers`).

## Configuração operacional confirmada

Valores confirmados como configuração operacional (não foram lidos
diretamente da infraestrutura de produção nesta sessão):

| Variável | Valor | Significado |
| --- | --- | --- |
| `UNIT_ECONOMICS_GATEWAY_FEE_PERCENT` | 4,98 | taxa **total** do pagamento à vista |
| `UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT` | 10 | taxa **total** do pagamento parcelado |
| `UNIT_ECONOMICS_TAX_PERCENT` | 5 | imposto sobre a base cobrada |
| `UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER` | 3,98 | custo fixo por pedido (R$) |
| `UNIT_ECONOMICS_REFUND_RESERVE_PERCENT` | 3,98 | reserva de reembolso sobre a base cobrada |
| `UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED` | (ausente) | gate de margem de publicação hard-off (só o literal `1` habilita, `publication.py:57-59`) |
| `UNIT_ECONOMICS_MIN_OPERATIONAL_MARGIN_PERCENT` | (não confirmado) | mínimo do gate de publicação (`publication.py:52-54`) |

As taxas 4,98% e 10% são **alternativas por modalidade**, nunca somáveis entre
si. Sem `payment_scenario`, o construtor aplica as duas (`gateway_fee` +
`installment_cost`), que é exatamente o contrato legado a ser corrigido.

## Inventário de consumidores

`build_unit_economics_components` vive em `loja/services/unit_economics_policy.py`.
Ramos relevantes: retenção observada, cenário único (`payment_scenario`),
legado aditivo (`payment_scenario is None`).

| # | Consumidor | Local | Entrada / rota | `payment_scenario` | Comportamento |
| --- | --- | --- | --- | --- | --- |
| 1 | Painel de prontidão | `sales_readiness.py` | `GET /admin/prontidao-vendas` | loop `"cash"` + `"installment"` | **Corrigido (PR #74)**: duas margens + menor; PRONTO exige ambas |
| 2 | Adapter de pedido | `order_unit_economics.py` (`evaluate_order_unit_economics` / `_with_payment`) | consumidores com Payment | retém legado em `_from_snapshot`; `_with_payment` usa retenção → cenário → fail-closed | **Corrigido no wiring** (`_with_payment`); legado preservado só no caminho sem evidência |
| 3 | Métricas por canal | `channel_metrics.py` → `select_order_payment` + `_with_payment` | `GET /admin/metricas-por-canal` | evidência por pedido | **Corrigido**: retenção → cenário → `unknown` (nunca aditivo) |
| 4 | Gate de publicação | `publication_unit_economics.py` | `publication.py`, só com `UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED=1` | loop cash + installment | **Corrigido (hard-off)**: margem mínima em ambos; pior cenário em `evaluation` |
| 5 | Dashboard de unit economics | `publication_unit_economics_dashboard.py` | `GET /admin/unit-economics` | loop cash + installment | **Corrigido**: colunas à vista/parcelada; consolidado = pior cenário |

Notas do inventário:

- `_with_payment` é o adapter com precedência retenção → cenário →
  fail-closed; hoje tem chamador de produção: `channel_metrics`.
- `_from_snapshot` (sem Payment) permanece aditivo legado e é usado apenas
  por chamadores que não passam evidência — não é o caminho do painel de canal.
- Nenhum consumidor altera fatos do pedido: subtotal, shipping, desconto, total
  e os snapshots de custo dos itens são lidos como estão; margens são derivadas
  e recalculadas a cada leitura nos painéis administrativos.

## Impacto do contrato aditivo legado (configuração confirmada)

Com as taxas confirmadas, o legado cobra **14,98%** de custo de pagamento sobre
a base cobrada (4,98% + 10%) em vez de 4,98% (à vista) **ou** 10% (parcelada).

Ilustração aritmética com o mesmo exemplo de `PAYMENT_SCENARIOS_READINESS.md`
(preço/cobrança R$ 164,56; posto R$ 115,19; sem desconto) — é aritmética de
exemplo, **não** é uma medição da produção atual:

| Cenário | Custo de pagamento | Custos totais | Margem operacional | % |
| --- | --- | --- | --- | --- |
| À vista (4,98%) | R$ 8,20 | R$ 26,96 | R$ 22,41 | 13,62% |
| Parcelada (10%) | R$ 16,46 | R$ 35,22 | R$ 14,15 | 8,60% |
| **Legado aditivo (4,98% + 10%)** | **R$ 24,66** | **R$ 43,42** | **R$ 5,95** | **3,62%** |

Custos comuns a todos os cenários: imposto R$ 8,23 + aquisição R$ 3,98 +
reserva R$ 6,55 = R$ 18,76.

Efeitos:

- **Dashboards de admin** (`/admin/unit-economics`, `/admin/metricas-por-canal`):
  margem sistematicamente subestimada — R$ 16,46 abaixo do cenário à vista e
  R$ 8,20 abaixo do cenário parcelado neste exemplo. Direção: falso pessimismo.
- **Gate de publicação** (quando habilitado): avalia a margem com as duas taxas
  empilhadas, portanto um produto viável em qualquer modalidade real pode ser
  bloqueado (`operational_margin_below_minimum`) sem sê-lo de fato. O viés é
  conservador (bloqueia mais, nunca libera errado), mas decide com número
  incorreto.
- **Painel de prontidão**: imune (PR #74).

## Evidência de modalidade persistida

- `Payment` **não** possui coluna `installments`
  (`payment_models.py:421-458`). A evidência candidata é
  `Payment.raw_payload` (`:454-458`), que persiste o objeto JSON completo da
  API de pagamento do Mercado Pago (`payments_routes.py:934/944`).
- A API de pagamento do Mercado Pago expõe o campo `installments`, mas **isto
  ainda não foi verificado contra um payload persistido real**. O resolvedor e
  fixtures de teste cobrem o contrato; a verificação em produção é a pendência
  P1 (script read-only abaixo).
- `Payment.payment_type_id` (`payment_models.py:452`) existe, mas é evidência
  **insuficiente**: `credit_card` não distingue compra de 1x (à vista) de
  parcelada, e tipos como transferência não comprovam a contagem de parcelas
  sem o campo numérico. Não deve ser usado sozinho para inferir cenário.
- `resolve_payment_net_receipt` (`payment_net_receipt.py`) devolve a retenção
  observada do processador para pagamentos Mercado Pago aprovados com
  `transaction_details.net_received_amount`; é independente de modalidade e
  tem precedência sobre qualquer estimativa — acionada por
  `evaluate_order_unit_economics_with_payment` (hoje via `channel_metrics`).
- Vínculo pedido↔pagamento existe: `Order.payments`
  (`payment_models.py:226-232`) e `Payment.order_id`.

## Fatos históricos permanecem preservados

- Nenhum campo monetário de `Order` / `OrderItem` é alterado por esta entrega
  ou pela proposta: subtotal, shipping, desconto, total e snapshots de custo
  congelados são imutáveis.
- Nenhuma margem é persistida: os painéis recalculam a cada leitura; corrigir o
  cenário muda apenas o número derivado exibido.
- A proposta não altera preço, checkout, reserva de estoque, fulfillment,
  publicação automática, chamadas a provider nem variáveis novas.

## Correção aplicada (precedência)

Contrato de precedência para **pedidos** (mesma semântica do PR #74 e do
construtor):

1. **Retenção observada** (`resolve_payment_net_receipt`) quando o Payment
   aprovado a comprova → substitui as estimativas.
2. Senão **evidência de modalidade** (`raw_payload["installments"]` inteiro ≥ 1)
   → um único `payment_fee` da modalidade correspondente.
3. Senão → custos de pagamento **desconhecidos** (componentes `unknown`) → margem
   `None` / avaliação incompleta, contada como desconhecida e nunca como zero.

Nunca: somar as duas taxas; inferir modalidade sem evidência; inventar valores.

Por consumidor:

1. **Painel de prontidão** — já correto (PR #74). Nenhuma mudança.
2. **Adapter de pedido** — `payment_scenario` opcional em
   `evaluate_order_unit_economics`; `_with_payment` usa retenção → cenário →
   fail-closed (campos de pagamento da política viram `None`). O teste legado
   de fallback aditivo foi revisado.
3. **Métricas por canal** — `Order.payments` eager na rota,
   `select_order_payment` + `_with_payment`. Pedido sem Payment elegível →
   margem desconhecida, não aditivo.
4. **Gate de publicação** — avalia **os dois cenários** e exige a margem mínima
   em ambos (hard-off até `UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED=1`).
5. **Dashboard `/admin/unit-economics`** — colunas à vista / parcelada;
   consolidado = pior cenário quando ambos completos.
6. **`resolve_payment_scenario` + `select_order_payment`** — resolvedores
   read-only e fail-closed em `loja/services/payment_scenario.py`, ligados nos
   consumidores acima.

## Alterações desta entrega (branch `audit/payment-scenario-consumers`)

- `docs/PAYMENT_SCENARIOS_LEGACY_AUDIT.md` (este documento).
- `loja/services/payment_scenario.py` — resolvedor + `select_order_payment`.
- `loja/services/order_unit_economics.py` — `payment_scenario` + fail-closed
  em `_with_payment`.
- `loja/services/channel_metrics.py` + `loja/views/admin_routes.py` — wiring
  com Payment eager.
- `loja/services/publication_unit_economics.py` — gate cenário duplo.
- `loja/services/publication_unit_economics_dashboard.py` +
  `loja/templates/admin/unit_economics_dashboard.html` — colunas de cenário.
- `loja/services/unit_economics_policy.py` — constantes de cenário.
- `scripts/verify_payment_evidence_readonly.py` — P1/P6 read-only.
- `tests/*` — cobertura dos caminhos novos/revisados.

Fora do escopo (propositalmente): migração, novas variáveis, consultas a
provider, alteração de fulfillment/checkout/preço, cotação de frete ao vivo.

## Pendências

Status nesta sessão (correções implementadas no branch
`audit/payment-scenario-consumers`):

1. **Verificar num payload Mercado Pago persistido real** que
   `raw_payload["installments"]` existe e é inteiro.
   **Pendente de execução** (sem `DATABASE_URL` neste ambiente): script
   somente leitura `scripts/verify_payment_evidence_readonly.py` (P1 + P6),
   schema-qualificado, agregado, sem PII.
2. Tratamento de pedidos **sem** evidência: **Resolvido** — fail-closed
   (`evaluate_order_unit_economics_with_payment` zera os campos de pagamento
   da política; componentes `unknown`; nunca aditivo). Afeta
   `/admin/metricas-por-canal` (`margin_known` só com Payment elegível).
3. Regra de seleção do Payment por pedido: **Resolvido** —
   `select_order_payment` em `loja/services/payment_scenario.py`
   (Mercado Pago + `approved` + `amount == order.total`, mais recente;
   sem candidato → `None`). Ligado em `channel_metrics` + eager
   `Order.payments` na rota.
4. Autorização para o gate de publicação (cenário duplo): **Resolvido**
   (autorizado; implementado). `evaluate_product_net_margin` avalia cash e
   installment e exige a margem mínima em **ambos**; pior cenário alimenta
   `evaluation`. Gate continua hard-off (`UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED`
   só com literal `1`).
5. Redesign de colunas do dashboard `/admin/unit-economics`: **Resolvido** —
   colunas à vista / parcelada + margem consolidada = pior cenário quando
   ambos completos (`unit_economics_dashboard.html`).
6. Revalidar a auditoria anterior de produtos/custos/estoque:
   **Script pronto, execução pendente** — mesma saída do item 1
   (`scripts/verify_payment_evidence_readonly.py`).
7. Fornecedor de frete segue bloqueado (`loja/integrations/aliexpress/FREIGHT_PROBE.md`
   no início de qualquer sessão de cotação): sem cotação ao vivo nesta entrega;
   `SHIPPING_TEMPORARY_FLAT_RATE` R$ 10,00 continua sendo receita temporária,
   nunca prova de custo logístico. **Inalterado.**
8. `gh` CLI indisponível nesta sessão: o PR será aberto pela URL de comparação
   do GitHub após o push do branch.
   https://github.com/sharktank-tech/PolloModas/pull/new/audit/payment-scenario-consumers
