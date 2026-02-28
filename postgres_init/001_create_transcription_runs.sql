-- 1) ENUM (idempotente)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transcription_status') THEN
    CREATE TYPE transcription_status AS ENUM ('CREATED','PENDING','WORKING','COMPLETED','ERROR');
  END IF;
END$$;

-- 2) Tabella (idempotente)
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


-- 4) Indici (idempotenti)
CREATE INDEX IF NOT EXISTS idx_transcription_runs_status ON public.transcription_runs(status);
CREATE INDEX IF NOT EXISTS idx_transcription_runs_created_at ON public.transcription_runs(created_at);
