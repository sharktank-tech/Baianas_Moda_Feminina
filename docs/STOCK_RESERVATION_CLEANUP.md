# Liberação de reservas vencidas

## Objetivo

Restituir estoque de pedidos que continuam em `pending_payment` após a janela
operacional. A rotina não cancela o pedido, não invalida a preferência do
Mercado Pago, não consulta o provedor e não chama fornecedores.

## Configuração

- `STOCK_RESERVATION_RELEASE_ENABLED=1`: habilita somente o cron automático.
  Ausente ou diferente de `1` mantém o cron fail-closed.
- `STOCK_RESERVATION_HOURS=24`: janela entre 1 e 720 horas.
- `STOCK_RESERVATION_RELEASE_LIMIT=100`: lote entre 1 e 500 pedidos.
- `CRON_SECRET`: segredo gerenciado pela Vercel. Nunca versionar ou imprimir.

O cron chama uma vez por dia, às 09:17 UTC (06:17 em Palmas/TO), frequência compatível com o plano Vercel Hobby:

`GET /internal/maintenance/release-expired-reservations`

A rota exige `Authorization: Bearer <CRON_SECRET>`. A resposta contém apenas
contagens agregadas.

## Concorrência e idempotência

1. A rotina seleciona IDs candidatos.
2. Cada pedido é relido com `FOR UPDATE SKIP LOCKED`.
3. Sob o lock, confirma novamente:
   - `status == pending_payment`;
   - `stock_reserved == true`;
   - idade acima da janela.
4. Produto e variante são travados e restituídos.
5. Somente `stock_reserved` passa a `false`; o status permanece
   `pending_payment`.

Se o pagamento vencer a corrida, a limpeza pula o pedido. Se a limpeza vencer,
um pagamento tardio segue o fluxo existente e pode reservar novamente sob lock,
sem débito duplo.

## Operação administrativa

A página `/admin/recuperacao-checkout` mostra somente a contagem de reservas
vencidas e oferece um POST protegido por login administrativo e CSRF.
A ação humana independe da flag do cron, mas usa o mesmo serviço idempotente.

Logs registram apenas `source`, `candidates`, `released` e `skipped`.
Nenhum UUID, e-mail, CPF, endereço ou payload de pagamento é registrado.

## Verificação de um checkout de smoke após 24 horas

Mantenha a referência do pedido somente no canal operacional privado. Após
completar 24 horas:

1. abrir o painel de recuperação;
2. confirmar que aparece na contagem de reservas vencidas, se a reserva ainda
   estiver ativa;
3. executar a ação administrativa ou aguardar o cron habilitado;
4. confirmar somente:
   - pedido ainda `pending_payment`;
   - `stock_reserved=false`;
   - estoque restituído exatamente uma vez;
   - nenhuma chamada Mercado Pago/AliExpress.

Não publicar número/UUID do pedido, preferência, CPF, endereço ou dados do
cliente.
