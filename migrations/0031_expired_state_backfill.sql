-- Item 1.1 (fechamento): pedidos cuja reserva JÁ foi devolvida antes do
-- estado `expired` existir (PR #90, merge 3815818) permaneciam em
-- `pending_payment` para sempre — todos os caminhos de liberação exigem
-- `stock_reserved = true`, então nenhum deles os alcança mais.
--
-- Este backfill aplica a MESMA transição da liberação (`status -> 'expired'`)
-- com os mesmos gates fail-closed:
--
--   * reserva já devolvida (`stock_reserved = false`);
--   * prova de expiração: `mp_preference_expires_at` preenchido e vencido
--     (NULL = sem prova = intocado, fiel à0024/0030);
--   * NENHUM pagamento aprovado registrado (gate conservador: se houver
--     qualquer linha `approved`, o webhook ainda pode promover o pedido e o
--     backfill não interfere — pagamento decide, não a ausência de prazo).
--
-- Não toca em reservas ativas (`stock_reserved = true`): o cron/liberação
-- com lock do código #90 cuida delas com revalidação por linha.
--
-- Idempotente: o WHERE fica vazio na reaplicação (o status deixa de ser
-- `pending_payment`).

UPDATE baianas_moda_feminina.orders
SET status = 'expired'
WHERE status = 'pending_payment'
  AND stock_reserved = false
  AND mp_preference_expires_at IS NOT NULL
  AND mp_preference_expires_at < now()
  AND NOT EXISTS (
      SELECT 1
      FROM baianas_moda_feminina.payments p
      WHERE p.order_id = orders.id
        AND p.status = 'approved'
  );
