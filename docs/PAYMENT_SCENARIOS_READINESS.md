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
Sem esse argumento, permanece o contrato aditivo legado dos consumidores de
pedidos e publicação. Esta entrega corrige exclusivamente a simulação de
prontidão; não reinterpreta pedidos sem evidência da modalidade nem altera gates
de publicação. A retenção real do processador, quando fornecida, substitui as
estimativas em qualquer cenário. Não há migração nem novas variáveis.

Ver também `PAYMENT_SCENARIOS_LEGACY_AUDIT.md` (inventário dos consumidores
legados de `payment_scenario` e proposta de correção).
