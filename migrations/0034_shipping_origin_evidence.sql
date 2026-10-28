-- Origem de envio por variante com evidência (Etapa 1 da integração
-- AliExpress: origem BR/exterior/desconhecida).
--
-- Duas escritas separadas, nunca misturadas:
--   * manual_origin: classificação confirmada pelo admin (exige evidence_ref +
--     confirmed_at por CHECK). É a única fonte que um humano declara.
--   * api_origin: valor observado em dado do fornecedor (exige
--     api_observed_at). Nunca sobrescreve a confirmação manual: as duas
--     colunas convivem e o conflito entre elas é sinalizado, não silenciado.
--
-- Escopo por linha: supplier_sku_id (SKU específico) OU covers_all_variants
-- (cobertura explícita das variantes do vínculo). A resolução usa SKU com
-- precedência sobre a cobertura. Uma linha BR isolada NUNCA torna o produto
-- "nacional": o produto só é nacional quando TODAS as variantes vendáveis
-- selecionadas estiverem comprovadas como BR.
--
-- Nada é backfillado: ausência de linha = origem desconhecida (fail-closed).
-- Idempotente. DOWN em 0034_shipping_origin_evidence_down.sql.

CREATE TABLE IF NOT EXISTS baianas_moda_feminina.supplier_sku_shipping_origins (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_product_id uuid NOT NULL
        REFERENCES baianas_moda_feminina.supplier_products(id) ON DELETE CASCADE,
    supplier_sku_id varchar(64),
    covers_all_variants boolean NOT NULL DEFAULT FALSE,
    api_origin varchar(16),
    manual_origin varchar(16),
    origin_country varchar(2),
    evidence_ref text,
    confirmed_by_user_id uuid
        REFERENCES baianas_moda_feminina.users(id) ON DELETE SET NULL,
    confirmed_at timestamp without time zone,
    api_observed_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_shipping_origin_scope
        CHECK (
            (supplier_sku_id IS NOT NULL AND covers_all_variants = FALSE)
            OR (supplier_sku_id IS NULL AND covers_all_variants = TRUE)
        ),
    CONSTRAINT ck_shipping_origin_api_origin
        CHECK (api_origin IS NULL OR api_origin IN ('brazil', 'foreign')),
    CONSTRAINT ck_shipping_origin_manual_origin
        CHECK (manual_origin IS NULL OR manual_origin IN ('brazil', 'foreign')),
    CONSTRAINT ck_shipping_origin_manual_evidence
        CHECK (
            manual_origin IS NULL
            OR (evidence_ref IS NOT NULL AND confirmed_at IS NOT NULL)
        ),
    CONSTRAINT ck_shipping_origin_api_evidence
        CHECK (api_origin IS NULL OR api_observed_at IS NOT NULL),
    CONSTRAINT ck_shipping_origin_country
        CHECK (origin_country IS NULL OR origin_country ~ '^[A-Za-z]{2}$')
);

-- Uma linha por escopo: SKU específico ou cobertura total do vínculo.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shipping_origin_variant_sku
    ON baianas_moda_feminina.supplier_sku_shipping_origins
    (supplier_product_id, supplier_sku_id)
    WHERE supplier_sku_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipping_origin_cover_all
    ON baianas_moda_feminina.supplier_sku_shipping_origins (supplier_product_id)
    WHERE covers_all_variants;

-- Seleção de operação nacional por variante: somente a variante marcada
-- participa do gate de operação nacional (catálogo internacional não
-- selecionado nunca bloqueia). Aditivo, com default, sem backfill.
ALTER TABLE baianas_moda_feminina.product_variants
    ADD COLUMN IF NOT EXISTS national_shipping_operation boolean
    NOT NULL DEFAULT FALSE;
