-- Item 1.1: backfill do prazo de expiração da reserva para pedidos legados.
--
-- A 0024 criou mp_preference_expires_at e deixou os pedidos existentes NULL
-- de propósito; o código só passou a gravar o prazo em 2026-09-21
-- (reservation_deadline). Todo pending_payment com prazo NULL nunca aparece
-- em find_expired_stock_reservations (falha fechada), então a reserva de
-- estoque ficava travada para sempre e o cron diário de liberação nunca agia
-- sobre essas linhas (counter count_unproven_stock_reservations).
--
-- Backfill canônico: prazo = created_at + 24h (DEFAULT_RESERVATION_HOURS em
-- loja/services/reservation_policy.py — manter em sincronia se a política
-- mudar). Liberar continua fail-closed: a liberação só devolve estoque de
-- pedidos sem pagamento aprovado (latest-first), então preencher o prazo não
-- arrisca pedidos pagos.
--
-- Idempotente: reaplicar não altera linhas (conjunto WHERE fica vazio).
-- A liberação em si acontece no próximo cron (09:17 UTC) ou pelo botão
-- admin de liberação em lote.

UPDATE pollomodas.orders
SET mp_preference_expires_at = created_at + interval '24 hours'
WHERE status = 'pending_payment'
  AND mp_preference_expires_at IS NULL;
