-- Reversão do backfill 0030.
--
-- Restaura para NULL somente as linhas que o backfill poderia ter escrito:
-- pending_payment criadas ANTES da migração 0024 (2026-09-21), quando a
-- coluna ainda não existia — qualquer valor não nulo nessas linhas veio do
-- backfill. Linhas criadas em/aa 2026-09-21 em diante podem ter o prazo
-- gravado pelo código de criação do pedido (mesma assinatura created_at +
-- 24h) e são preservadas.
--
-- Reversão documentada como parcial: linhas legadas criadas no mesmo dia da
-- implantação da 0024 também foram preenchidas e não são revertidas aqui
-- (inofensivo; reaplicar o UP é idempotente).

UPDATE baianas_moda_feminina.orders
SET mp_preference_expires_at = NULL
WHERE status = 'pending_payment'
  AND created_at < DATE '2026-09-21'
  AND mp_preference_expires_at = created_at + interval '24 hours';
