# Runbook — primeira venda real

Procedimento manual para a primeira venda com risco controlado. Nenhuma
etapa é automática: cada verificação exige conferência humana explícita.

## Fluxo

1. Aguarde o **pagamento canônico aprovado** no Mercado Pago (webhook
   validado, status `approved`).
2. Confirme o pedido em estado **paid** no painel administrativo.
3. Confira **reserva e estoque**: reserva ativa, quantidades e variantes
   íntegras entre pedido, produto e variante.
4. Confira **endereço/snapshot** do pedido sem publicar PII (sem expor
   CPF, telefone ou endereço fora do painel autenticado).
5. Obtenha novamente **custo e disponibilidade do fornecedor** (canal
   oficial do fornecedor, fora da loja).
6. Compare com a **economia aprovada** (custo, moeda, margem, frete).
7. **Interrompa** se preço, moeda, SKU, quantidade ou frete divergirem —
   não prossiga para compra.
8. **Compra no fornecedor somente após autorização financeira explícita**
   (responsável nomeado, por escrito, com valor e prazo).
9. Registre o **rastreamento** no pedido.
10. Acompanhe **reconciliação e atendimento ao cliente** até a entrega.

## Avisos permanentes

- **Rollback local não desfaz compra externa**: estornar ou cancelar no
  painel não cancela o pedido feito ao fornecedor.
- **Pagamento estornado bloqueia fulfillment**: `refunded`/`charged_back`
  nunca disparam compra ou envio.
- **Gates AliExpress continuam desligados**: fulfillment automático não
  existe; todo envio de fornecedor é manual e autorizado caso a caso.
- **Uma autorização para merge não autoriza compra no fornecedor**:
  código aprovado não é ordem de compra.
- A tarifa temporária de R$ 10,00 é receita/cobrança, não prova de custo
  logístico; divergências contra o custo real são esperadas e devem ser
  conferidas no passo 6.
- PRONTO no painel vale apenas para o cenário logístico de referência
  (CEP comercial + opção + quantidade 1 configurados), nunca para
  qualquer endereço do Brasil: antes de divulgar para outra região,
  reavalie a prova de frete para o destino aplicável.


## Cotação persistida do cenário de referência

- A tarifa temporária de R$ 10,00 continua sendo cobrança comercial; nunca prova
  custo do fornecedor.
- O painel lê somente cotações vigentes da tabela canônica por vínculo, prefixo
  de CEP, opção e quantidade. O GET não consulta AliExpress.
- Uma consulta futura só pode entrar por provider read-only explicitamente
  injetado e gateado. Não existe provider padrão nesta entrega.
- A cotação não habilita fulfillment, placeOrder, Mercado Pago, reserva de
  estoque, alteração de preço ou publicação.
- PRONTO vale somente para o prefixo/opção/quantidade comprovados. Cotação
  vencida, moeda divergente ou janela inválida falha fechado.
- Rollback local nunca desfaz uma operação externa; por isso nenhuma compra é
  executada durante a coleta de evidência logística.
