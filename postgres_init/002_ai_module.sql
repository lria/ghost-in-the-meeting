-- ============================================================
-- 002_ai_module.sql
-- Modulo AI: Speaker Rename + Minuta Generation + RAG
--
-- IDEMPOTENTE — sicuro da eseguire:
--   • fresh install (dopo 001, eseguito automaticamente da postgres_init)
--   • upgrade su DB esistente (eseguire manualmente una volta)
--
-- Comando upgrade manuale:
--   docker exec -i n8n_stack_postgres psql -U n8n -d n8n \
--     < postgres_init/002_ai_module.sql
--
-- Dipendenze: richiede 001_create_transcription_runs.sql eseguito prima.
-- Su fresh install l'ordine alfabetico garantisce 001 → 002.
-- ============================================================


-- ── 1. Colonne aggiuntive su wx_transcription_jobs ───────────────────────────
ALTER TABLE public.wx_transcription_jobs
  ADD COLUMN IF NOT EXISTS output_minutes_url text,
  ADD COLUMN IF NOT EXISTS output_txt_url     text,
  ADD COLUMN IF NOT EXISTS speakers_mapped    boolean NOT NULL DEFAULT false;


-- ── 2. Funzione update_updated_at (idempotente con OR REPLACE) ───────────────
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


-- ── 3. speaker_aliases ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.speaker_aliases (
  id           serial      PRIMARY KEY,
  job_id       text        NOT NULL
                             REFERENCES public.wx_transcription_jobs(job_id)
                             ON DELETE CASCADE,
  speaker_id   text        NOT NULL,
  name         text        NOT NULL,
  company      text,
  customer     text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, speaker_id)
);

CREATE INDEX IF NOT EXISTS idx_speaker_aliases_job_id
  ON public.speaker_aliases(job_id);

CREATE INDEX IF NOT EXISTS idx_speaker_aliases_customer
  ON public.speaker_aliases(customer);

DROP TRIGGER IF EXISTS trg_speaker_aliases_updated_at ON public.speaker_aliases;
CREATE TRIGGER trg_speaker_aliases_updated_at
  BEFORE UPDATE ON public.speaker_aliases
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


-- ── 4. minutes_jobs ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.minutes_jobs (
  minutes_job_id  text        PRIMARY KEY,
  job_id          text        NOT NULL
                                REFERENCES public.wx_transcription_jobs(job_id)
                                ON DELETE CASCADE,
  status          text        NOT NULL DEFAULT 'PENDING',
  template        text        NOT NULL DEFAULT 'standard',
  model           text        NOT NULL DEFAULT '',
  use_rag         boolean     NOT NULL DEFAULT true,
  rag_chunks_used int,
  rag_query       text,
  output_url      text,
  qdrant_indexed  boolean     NOT NULL DEFAULT false,
  error_message   text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz
);

CREATE INDEX IF NOT EXISTS idx_minutes_jobs_job_id
  ON public.minutes_jobs(job_id);

CREATE INDEX IF NOT EXISTS idx_minutes_jobs_status
  ON public.minutes_jobs(status);

CREATE INDEX IF NOT EXISTS idx_minutes_jobs_created_at
  ON public.minutes_jobs(created_at DESC);

DROP TRIGGER IF EXISTS trg_minutes_jobs_updated_at ON public.minutes_jobs;
CREATE TRIGGER trg_minutes_jobs_updated_at
  BEFORE UPDATE ON public.minutes_jobs
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


-- ── 5. View: jobs con ultima minuta ──────────────────────────────────────────
-- NOTA: la view viene creata DOPO le ALTER TABLE così tutte le colonne
-- sono già visibili al momento della compilazione della view.
-- output_txt_url è inclusa qui perché aggiunta dall'ALTER sopra.
CREATE OR REPLACE VIEW public.v_jobs_with_minutes AS
SELECT
  j.job_id,
  j.customer,
  j.project,
  j.status              AS transcription_status,
  j.language,
  j.speakers_mapped,
  j.input_filename,
  j.output_json_url,
  j.output_txt_url,
  j.output_minutes_url,
  j.created_at,
  j.finished_at,
  mj.minutes_job_id,
  mj.status             AS minutes_status,
  mj.model              AS minutes_model,
  mj.template           AS minutes_template,
  mj.use_rag,
  mj.rag_chunks_used,
  mj.output_url         AS minutes_url,
  mj.qdrant_indexed,
  mj.created_at         AS minutes_created_at,
  mj.finished_at        AS minutes_finished_at
FROM public.wx_transcription_jobs j
LEFT JOIN public.minutes_jobs mj
  ON mj.minutes_job_id = (
    SELECT minutes_job_id
    FROM   public.minutes_jobs
    WHERE  job_id = j.job_id
    ORDER  BY created_at DESC
    LIMIT  1
  );


-- ── 6. Verifica finale ────────────────────────────────────────────────────────
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'wx_transcription_jobs',
    'speaker_aliases',
    'minutes_jobs'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = t
    ) THEN
      RAISE EXCEPTION '002_ai_module: tabella % mancante dopo migrazione', t;
    END IF;
  END LOOP;
  RAISE NOTICE '002_ai_module: migrazione completata con successo.';
END$$;