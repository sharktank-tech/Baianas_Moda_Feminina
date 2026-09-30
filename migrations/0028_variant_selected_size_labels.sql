-- Rótulos completos de variante em selected_size (caso "Opção N" / tamanhos
-- longos, ex.: "11-12T(Size 160)").
-- Amplia VARCHAR sem reescrever valores: snapshots existentes (pedidos
-- antigos, carrinho vigente) são preservados integralmente; o código passa a
-- gravar o rótulo resolvido (coluna ou attributes) e falha de forma explícita
-- acima de 40 caracteres — nunca há truncamento silencioso.
-- Idempotente (reaplicável); DOWN reversível em 0028_variant_selected_size_labels_down.sql.
-- Aplicar ANTES do deploy do código que grava rótulos completos.

ALTER TABLE pollomodas.cart_items
    ALTER COLUMN selected_size TYPE varchar(40);

ALTER TABLE pollomodas.order_items
    ALTER COLUMN selected_size TYPE varchar(40);
