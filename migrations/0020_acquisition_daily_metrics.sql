-- Migration 0020: sessões agregadas por canal (FASE 9.0.0-G)
\set ON_ERROR_STOP on
SET search_path TO pollomodas;

CREATE TABLE IF NOT EXISTS acquisition_daily_metrics (
    id UUID PRIMARY KEY,
    day DATE NOT NULL,
    channel VARCHAR(140) NOT NULL,
    sessions INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_acquisition_daily_day_channel UNIQUE (day, channel),
    CONSTRAINT ck_acquisition_daily_sessions_nonnegative CHECK (sessions >= 0)
);

DO $$
BEGIN
    IF to_regclass('public.acquisition_daily_metrics') IS NOT NULL THEN
        RAISE EXCEPTION 'acquisition_daily_metrics unexpectedly exists in public';
    END IF;
END $$;
