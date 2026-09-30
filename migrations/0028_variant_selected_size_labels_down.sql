-- DOWN de 0028_variant_selected_size_labels.sql — reversível.
-- Restaura os limites anteriores. ATENÇÃO: falha se alguma linha tiver
-- selected_size com mais de 10 (cart_items) ou 20 (order_items) caracteres;
-- nesses casos, revisar os rótulos antes de reverter.

ALTER TABLE pollomodas.cart_items
    ALTER COLUMN selected_size TYPE varchar(10);

ALTER TABLE pollomodas.order_items
    ALTER COLUMN selected_size TYPE varchar(20);
