-- Escopo real da evidência de frete (P7, rota manual).
-- Estende supplier_reference_freight_quotes sem quebrar linhas legadas:
-- linhas sem escopo (dest_cep NULL) NUNCA casam no checkout (fail-closed);
-- só a referência/painel pode lê-las. Novas escritas do admin preenchem
-- sempre o escopo completo (CEP exato ou faixa, variante ou cobertura
-- explícita multi, teto de quantidade).
ALTER TABLE pollomodas.supplier_reference_freight_quotes
    ADD COLUMN IF NOT EXISTS dest_cep varchar(8),
    ADD COLUMN IF NOT EXISTS dest_cep_end varchar(8),
    ADD COLUMN IF NOT EXISTS variant_scope varchar(64),
    ADD COLUMN IF NOT EXISTS covers_all_variants boolean NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS qty_max integer;

ALTER TABLE pollomodas.supplier_reference_freight_quotes
    DROP CONSTRAINT IF EXISTS ck_supplier_reference_freight_quote_dest_cep;
ALTER TABLE pollomodas.supplier_reference_freight_quotes
    ADD CONSTRAINT ck_supplier_reference_freight_quote_dest_cep
    CHECK (dest_cep IS NULL OR dest_cep ~ '^[0-9]{8}$');

ALTER TABLE pollomodas.supplier_reference_freight_quotes
    DROP CONSTRAINT IF EXISTS ck_supplier_reference_freight_quote_dest_range;
ALTER TABLE pollomodas.supplier_reference_freight_quotes
    ADD CONSTRAINT ck_supplier_reference_freight_quote_dest_range
    CHECK (
        dest_cep_end IS NULL
        OR (
            dest_cep_end ~ '^[0-9]{8}$'
            AND dest_cep IS NOT NULL
            AND dest_cep_end >= dest_cep
        )
    );

ALTER TABLE pollomodas.supplier_reference_freight_quotes
    DROP CONSTRAINT IF EXISTS ck_supplier_reference_freight_quote_qty_max;
ALTER TABLE pollomodas.supplier_reference_freight_quotes
    ADD CONSTRAINT ck_supplier_reference_freight_quote_qty_max
    CHECK (qty_max IS NULL OR qty_max > 0);
