-- Reviewed application-only migration. DO NOT run against a production DB as
-- part of CI or serverless startup. Execute only with a separately approved
-- change against the dedicated project schema; public is never a target.
BEGIN;
SET LOCAL search_path TO pollomodas;
DO $$ BEGIN
  IF current_schema() IS DISTINCT FROM 'pollomodas' THEN
    RAISE EXCEPTION 'dedicated project schema required';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS canary_evidence_cases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id uuid NOT NULL UNIQUE REFERENCES pollomodas.orders(id) ON DELETE RESTRICT,
  purpose varchar(20) NOT NULL DEFAULT 'simulation',
  state varchar(16) NOT NULL DEFAULT 'pending',
  created_at timestamp NOT NULL DEFAULT now(),
  decided_at timestamp,
  CONSTRAINT ck_canary_evidence_case_state CHECK (state IN ('pending','confirmed','rejected')),
  CONSTRAINT ck_canary_evidence_case_purpose CHECK (purpose IN ('simulation','organic_candidate'))
);
CREATE TABLE IF NOT EXISTS canary_evidence_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id uuid NOT NULL REFERENCES pollomodas.canary_evidence_cases(id) ON DELETE RESTRICT,
  actor_id uuid NOT NULL REFERENCES pollomodas.users(id) ON DELETE RESTRICT,
  kind varchar(16) NOT NULL,
  result varchar(16) NOT NULL,
  evidence_ref uuid,
  created_at timestamp NOT NULL DEFAULT now(),
  CONSTRAINT ck_canary_evidence_event_kind_result CHECK (
    (kind = 'technical' AND result IN ('passed','failed')) OR
    (kind = 'payment' AND result IN ('confirmed','divergent')) OR
    (kind = 'customer' AND result IN ('confirmed','denied','no_response')) OR
    (kind = 'decision' AND result IN ('confirmed','rejected'))
  ),
  CONSTRAINT ck_canary_evidence_event_reference CHECK (
    (kind IN ('payment','customer') AND evidence_ref IS NOT NULL) OR
    (kind IN ('technical','decision') AND evidence_ref IS NULL)
  )
);
CREATE INDEX IF NOT EXISTS ix_canary_evidence_events_case_id
  ON pollomodas.canary_evidence_events(case_id);

CREATE OR REPLACE FUNCTION pollomodas.canary_evidence_reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  RAISE EXCEPTION 'canary evidence events are append only';
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'pollomodas' AND c.relname = 'canary_evidence_events'
    AND t.tgname = 'canary_evidence_immutable'
  ) THEN
    CREATE TRIGGER canary_evidence_immutable
      BEFORE UPDATE OR DELETE ON pollomodas.canary_evidence_events
      FOR EACH ROW EXECUTE FUNCTION pollomodas.canary_evidence_reject_mutation();
  END IF;
END $$;

REVOKE ALL ON TABLE pollomodas.canary_evidence_cases,
  pollomodas.canary_evidence_events FROM PUBLIC;
COMMIT;
