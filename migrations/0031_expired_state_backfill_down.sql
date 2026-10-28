-- Reverte SOMENTE o backfill 0031: linhas que já estavam com reserva
-- devolvida e prazo vencido antes do dia da aplicação (2026-10-02), sem
-- pagamento aprovado.
--
-- Limitação documentada: sob o código do #90, linhas revertidas com
-- `stock_reserved = false` NÃO voltam a expirar sozinhas (os candidatos do
-- cron exigem reserva ativa) — usar o DOWN apenas em janela curta logo após
-- a aplicação. Linhas com pagamento aprovado nunca são tocadas.

UPDATE baianas_moda_feminina.orders
SET status = 'pending_payment'
WHERE status = 'expired'
  AND stock_reserved = false
  AND mp_preference_expires_at IS NOT NULL
  AND mp_preference_expires_at < DATE '2026-10-03'
  AND NOT EXISTS (
      SELECT 1
      FROM baianas_moda_feminina.payments p
      WHERE p.order_id = orders.id
        AND p.status = 'approved'
  );
