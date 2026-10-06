-- Divergência de custo/moeda em produto PUBLICADO (revisão do PR #123).
--
-- A sincronização técnica do publicado sinaliza a divergência em
-- `sync_error` e invalida a aprovação operacional marcando
-- `pricing_status='stale'` — sem tocar em preço, margem, conteúdo ou
-- publicação. A guarda criada em 0005 exigia `priced` para produto
-- AliExpress ATIVO, o que fazia o commit da sinalização ser rejeitado.
--
-- Agora `stale` também é aceito em produto publicado: o produto continua
-- à venda no banco, mas a VENDA fica bloqueada pelos controles canônicos
-- (loja.services.purchase_controls) e a republicação continua exigindo
-- `priced` (publication.evaluate_publication). Um simples
-- `is_active=true` em AliExpress NÃO precificado (unpriced/blocked/ready)
-- continua rejeitado.
--
-- Aditivo e idempotente; restrito ao schema `pollomodas`.
-- DOWN em 0035_published_divergence_guard_down.sql.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE products
    DROP CONSTRAINT IF EXISTS ck_product_aliexpress_publication_guard;

ALTER TABLE products
    ADD CONSTRAINT ck_product_aliexpress_publication_guard
    CHECK (
        source != 'aliexpress'
        OR is_active = false
        OR (
            is_draft = false
            AND pricing_status IN ('priced', 'stale')
        )
    );
