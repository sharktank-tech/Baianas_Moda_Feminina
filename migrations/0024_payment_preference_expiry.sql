-- Prazo canônico da preferência Mercado Pago e da reserva de estoque.
-- Pedidos legados permanecem NULL e não podem ser liberados automaticamente.
ALTER TABLE baianas_moda_feminina.orders
    ADD COLUMN IF NOT EXISTS mp_preference_expires_at timestamp without time zone;

CREATE INDEX IF NOT EXISTS ix_orders_mp_preference_expires_at
    ON baianas_moda_feminina.orders (mp_preference_expires_at)
    WHERE stock_reserved IS TRUE
      AND status = 'pending_payment'
      AND mp_preference_expires_at IS NOT NULL;
