-- ============================================================
-- 001_create_transcription_runs.sql
-- Schema condiviso: enum + tabella NeMo + tabella WhisperX
--
-- Idempotente: sicuro da rieseguire su DB esistente.
-- Aggiunge i valori mancanti all'enum senza distruggere dati.
-- ============================================================

-- ── 1. ENUM transcription_status ─────────────────────────────────────────────
-- Crea l'enum se non esiste, poi aggiunge i valori mancanti uno per uno.
-- ALTER TYPE ADD VALUE è idempotente solo con IF NOT EXISTS (PostgreSQL 9.6+)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transcription_status') THEN
    CREATE TYPE transcription_status AS ENUM
      ('CREATED','PENDING','WORKING','COMPLETED','FAILED','ERROR');
  ELSE
    -- Aggiunge i valori mancanti senza toccare quelli esistenti
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'CREATED') THEN
      ALTER TYPE transcription_status ADD VALUE 'CREATED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'PENDING') THEN
      ALTER TYPE transcription_status ADD VALUE 'PENDING';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'WORKING') THEN
      ALTER TYPE transcription_status ADD VALUE 'WORKING';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'COMPLETED') THEN
      ALTER TYPE transcription_status ADD VALUE 'COMPLETED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'FAILED') THEN
      ALTER TYPE transcription_status ADD VALUE 'FAILED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'transcription_status' AND e.enumlabel = 'ERROR') THEN
      ALTER TYPE transcription_status ADD VALUE 'ERROR';
    END IF;
  END IF;
END$$;


-- ── 2. Tabella NeMo transcription_runs ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.transcription_runs (
  run_id        text PRIMARY KEY,
  status        transcription_status NOT NULL DEFAULT 'CREATED',
  input_bucket  text NOT NULL,
  input_key     text NOT NULL,
  output_bucket text,
  output_prefix text,
  output_key    text,
  params_json   jsonb,
  error_message text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  finished_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_transcription_runs_status
  ON public.transcription_runs(status);
CREATE INDEX IF NOT EXISTS idx_transcription_runs_created_at
  ON public.transcription_runs(created_at);


-- ── 3. Tabella WhisperX wx_transcription_jobs ─────────────────────────────────
CREATE TABLE IF NOT EXISTS public.wx_transcription_jobs (
  job_id            text                 PRIMARY KEY,
  status            transcription_status NOT NULL DEFAULT 'PENDING',
  step              text,

  -- Contesto business
  customer          text                 NOT NULL DEFAULT '',
  project           text                 NOT NULL DEFAULT '',
  participants      text,

  -- Parametri trascrizione
  language          text                 NOT NULL DEFAULT 'it',
  max_speakers      int                  NOT NULL DEFAULT 8,
  diarize           boolean              NOT NULL DEFAULT true,

  -- Input / Output
  input_filename    text,
  output_json_url  text,
  error_message     text,

  -- Timestamps
  created_at        timestamptz          NOT NULL DEFAULT now(),
  updated_at        timestamptz          NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz
);

CREATE INDEX IF NOT EXISTS idx_wx_jobs_status
  ON public.wx_transcription_jobs(status);
CREATE INDEX IF NOT EXISTS idx_wx_jobs_customer_project
  ON public.wx_transcription_jobs(customer, project);
CREATE INDEX IF NOT EXISTS idx_wx_jobs_created_at
  ON public.wx_transcription_jobs(created_at DESC);


-- ── 4. Trigger updated_at (condiviso) ────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_transcription_runs_updated_at ON public.transcription_runs;
CREATE TRIGGER trg_transcription_runs_updated_at
  BEFORE UPDATE ON public.transcription_runs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_wx_jobs_updated_at ON public.wx_transcription_jobs;
CREATE TRIGGER trg_wx_jobs_updated_at
  BEFORE UPDATE ON public.wx_transcription_jobs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();