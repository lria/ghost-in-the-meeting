-- ============================================================
-- Schema WhisperX Jobs
-- File: 002_create_wx_transcription_jobs.sql
-- Eseguire dopo 001_create_transcription_runs.sql
-- ============================================================

-- Enum riutilizza quello già creato in 001 (transcription_status)
-- Se non esiste lo crea
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transcription_status') THEN
    CREATE TYPE transcription_status AS ENUM
      ('CREATED','PENDING','WORKING','COMPLETED','FAILED','ERROR');
  END IF;
END$$;

-- Tabella principale jobs WhisperX
CREATE TABLE IF NOT EXISTS public.wx_transcription_jobs (
  job_id            text                 PRIMARY KEY,
  status            transcription_status NOT NULL DEFAULT 'PENDING',
  step              text,

  -- Contesto business (per RAG e namespace)
  customer          text                 NOT NULL DEFAULT '',
  project           text                 NOT NULL DEFAULT '',
  participants      text,                          -- lista raw dal form

  -- Parametri trascrizione
  language          text                 NOT NULL DEFAULT 'it',
  max_speakers      int                  NOT NULL DEFAULT 8,
  diarize           boolean              NOT NULL DEFAULT true,

  -- Input
  input_filename    text,

  -- Output
  output_minio_url  text,                          -- es. http://minio:9000/wx-transcriptions/Alpitour/AI/job_id/result.json
  error_message     text,

  -- Timestamps
  created_at        timestamptz          NOT NULL DEFAULT now(),
  updated_at        timestamptz          NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz
);

-- Indici utili per query frequenti
CREATE INDEX IF NOT EXISTS idx_wx_jobs_status
  ON public.wx_transcription_jobs(status);

CREATE INDEX IF NOT EXISTS idx_wx_jobs_customer_project
  ON public.wx_transcription_jobs(customer, project);

CREATE INDEX IF NOT EXISTS idx_wx_jobs_created_at
  ON public.wx_transcription_jobs(created_at DESC);

-- ============================================================
-- Trigger: aggiorna updated_at automaticamente
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_wx_jobs_updated_at ON public.wx_transcription_jobs;
CREATE TRIGGER trg_wx_jobs_updated_at
  BEFORE UPDATE ON public.wx_transcription_jobs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
