-- Cotação vigente de frete por vínculo e cenário de referência.
-- Guarda somente prefixo de CEP e referência opaca; nunca endereço/CEP completo
-- nem payload ou credencial do fornecedor.
CREATE TABLE IF NOT EXISTS baianas_moda_feminina.supplier_reference_freight_quotes (
    supplier_product_id uuid NOT NULL
        REFERENCES baianas_moda_feminina.supplier_products(id) ON DELETE CASCADE,
    dest_prefix varchar(3) NOT NULL,
    option_key varchar(120) NOT NULL DEFAULT '',
    quantity integer NOT NULL,
    currency varchar(3) NOT NULL,
    cost numeric(12,2) NOT NULL,
    min_days integer,
    max_days integer,
    quoted_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    provider_method varchar(120) NOT NULL,
    evidence_ref_hash varchar(64),
    updated_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (supplier_product_id, dest_prefix, option_key, quantity),
    CONSTRAINT ck_supplier_reference_freight_quote_prefix
        CHECK (dest_prefix ~ '^[0-9]{3}$'),
    CONSTRAINT ck_supplier_reference_freight_quote_quantity
        CHECK (quantity > 0),
    CONSTRAINT ck_supplier_reference_freight_quote_currency
        CHECK (currency = 'BRL'),
    CONSTRAINT ck_supplier_reference_freight_quote_cost
        CHECK (cost >= 0),
    CONSTRAINT ck_supplier_reference_freight_quote_window
        CHECK (expires_at > quoted_at),
    CONSTRAINT ck_supplier_reference_freight_quote_days
        CHECK (
            (min_days IS NULL OR min_days >= 0)
            AND (max_days IS NULL OR max_days >= 0)
            AND (min_days IS NULL OR max_days IS NULL OR max_days >= min_days)
        )
);

CREATE INDEX IF NOT EXISTS ix_supplier_reference_freight_quotes_expiry
    ON baianas_moda_feminina.supplier_reference_freight_quotes (expires_at);
