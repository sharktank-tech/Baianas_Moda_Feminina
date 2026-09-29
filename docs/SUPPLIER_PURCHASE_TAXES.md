# Cotação completa de aquisição — tributos do fornecedor

O formulário de frete agora registra custo do produto, frete, imposto de
importação e ICMS, todos em BRL, com a mesma evidência de CEP, variante e
validade. Campos vazios são desconhecidos; zero exige confirmação do operador.
Não há cálculo de alíquotas tributárias nem inferência a partir do estado.

## Operação

1. Consulte o checkout do fornecedor para o destino/variante e uma unidade.
2. Guarde comprovante atualizado; informe os quatro valores separadamente.
3. Informe referência, validade operacional, CEP e variante. A data estimada
   de entrega não é validade de preço. Não há upload do comprovante; somente
   o hash da referência é persistido.
4. Salve no rascunho ou renove o produto publicado. Mudança material recalcula
   o publicado com a mesma margem-alvo, numa transação única. Confira o preço.
5. Simule o checkout no mesmo destino, variante e quantidade.

Nesta versão a evidência completa cobre **uma unidade por vínculo de
fornecedor**. Quantidades maiores são bloqueadas, inclusive duas linhas do
mesmo produto; exigem evolução para cotações de quantidade exata, sem
multiplicar um custo observado de uma unidade. Cobertura de faixa de CEP ou
todas as variantes só deve ser declarada quando comprovadamente tiver os
mesmos valores de produto, frete e tributos.

## Cálculo e histórico

Custo completo = produto + frete + imposto de importação + ICMS.
Preço = custo completo / (1 - margem-alvo/100), arredondado para cima.
A margem-alvo é anterior às despesas da venda. A política de pagamento,
tributos da venda, CAC e reserva permanece separada e inalterada.

Exemplo sintético baseado nos valores informados: 337,85 + 0 + 35,05 + 76,42
= 449,32; margem 30% => 641,89. Isso não cadastra uma evidência real, não
confirma o novo CEP e não autoriza uma compra.

Checkout consulta a evidência do banco e bloqueia impostos ausentes,
vencimento, destino/variante/quantidade não cobertos, mudança de custos ou
preço antigo no carrinho. Os quatro componentes, total e identificação
opaca da evidência ficam congelados no OrderItem antes de chamar o pagamento.
Atualizar o catálogo não muda pedidos anteriores. Pedidos antigos sem os
novos campos permanecem incompletos na avaliação financeira.

## Implantação

Aplicar `migrations/20260928161025_supplier_acquisition_taxes.sql` antes do
código. Migração aditiva, sem preencher impostos com zero, sem alterar preços,
sem dados de clientes e sem gates. Produtos já publicados continuam visíveis;
checkout AliExpress sem cotação completa bloqueia. Renovar as evidências após
o deploy. No caso do produto 1005009519310498, os valores precisam ser
confirmados pelo operador para o CEP atualizado antes do cadastro.

Testes não podem chamar provedores externos: fixture global bloqueia DNS e
conexões não locais; PostgreSQL descartável local continua permitido.
