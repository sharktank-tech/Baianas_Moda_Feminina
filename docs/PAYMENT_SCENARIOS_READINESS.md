# Cenários de pagamento no painel de prontidão

O painel `/admin/prontidao-vendas` simula duas modalidades independentes:

- À vista: `UNIT_ECONOMICS_GATEWAY_FEE_PERCENT` é a taxa total.
- Parcelada: `UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT` é a taxa total.

As duas taxas não são somadas nesta simulação. Imposto, aquisição por pedido e
reserva de reembolso continuam presentes em cada cenário. Não há valores
financeiros padrão. Use ponto como separador decimal nas variáveis.

A base é o preço do produto, quantidade 1. O frete cobrado do cliente continua
apenas informativo e não entra na receita simulada. O custo logístico segue as
mesmas exigências de evidência e validade do painel.

O painel exibe as duas margens percentuais e a menor margem. Dados incompletos
em qualquer cenário ou margem negativa em qualquer um bloqueiam PRONTO.
A ordenação de candidatos usa a menor das duas margens. Zero explícito é válido;
variável ausente não significa zero.

Exemplo aritmético, sem afirmar custo logístico atual: preço R$ 164,56, custo
posto R$ 115,19, taxas 4,98% à vista / 10% parcelada, imposto 5%, aquisição
R$ 3,98 e reserva 3,98% resultam em R$ 22,41 (13,62%) à vista e R$ 14,15 (8,60%)
parcelada, com arredondamento de cada componente a centavos.

## Escopo e compatibilidade

A seleção é explícita pelo argumento `payment_scenario` do construtor de custos.
Sem esse argumento, o construtor preserva o contrato aditivo legado (compatível
com consumidores antigos); os consumidores de produção de pedidos/publicação já
foram corrigidos no branch `audit/payment-scenario-consumers` (ver
`PAYMENT_SCENARIOS_LEGACY_AUDIT.md`): métricas por canal usam evidência de
pagamento fail-closed (elegibilidade MP+approved, vínculo/valor/payload
consistentes), o gate de publicação avalia os dois cenários (hard-off até
`UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED=1`) e o dashboard de unit economics
expõe colunas à vista/parcelada. A retenção real do processador, quando
fornecida, substitui as estimativas em qualquer cenário. Não há migração
nem novas variáveis nesta entrega de prontidão.

Ver também `PAYMENT_SCENARIOS_LEGACY_AUDIT.md` (inventário dos consumidores
legados de `payment_scenario` e status das correções — inclui o que é
implementado, testado com dados sintéticos e o que foi verificado em
produção; P1 e P6 já rodam production-verified com o script read-only).
