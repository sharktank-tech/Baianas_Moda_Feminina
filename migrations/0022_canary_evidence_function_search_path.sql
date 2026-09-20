-- Restrict name resolution for the private append-only evidence trigger.
-- Apply only after 0006_canary_evidence.sql in the dedicated schema.
BEGIN;
SET LOCAL search_path TO pollomodas;

DO $$
BEGIN
    IF current_schema() IS DISTINCT FROM 'pollomodas'
       OR to_regprocedure('pollomodas.canary_evidence_reject_mutation()') IS NULL THEN
        RAISE EXCEPTION 'dedicated evidence trigger function required';
    END IF;
END
$$;

ALTER FUNCTION pollomodas.canary_evidence_reject_mutation()
    SET search_path = pg_catalog, pollomodas;

COMMIT;
