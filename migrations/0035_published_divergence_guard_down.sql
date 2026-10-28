-- Reverte 0035: volta a exigir `priced` para produto AliExpress ATIVO.
--
-- Só execute após interromper a sinalização de divergência em produto
-- publicado (produto com `pricing_status='stale'` e `is_active=true`
-- passa a violar a guarda). Restringido a baianas_moda_feminina; idempotente.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE products
    DROP CONSTRAINT IF EXISTS ck_product_aliexpress_publication_guard;

ALTER TABLE products
    ADD CONSTRAINT ck_product_aliexpress_publication_guard
    CHECK (
        source != 'aliexpress'
        OR is_active = false
        OR (
            is_draft = false
            AND pricing_status = 'priced'
        )
    );
