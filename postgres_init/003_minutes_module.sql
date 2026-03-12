-- ============================================================
-- 003_minutes_module.sql
-- Modulo Minuta AI: RAG indexing + generazione minuta
--
-- Crea gli enum e le tabelle necessarie a rag_indexer.py,
-- minutes_worker.py e minutes_api.py.
-- Crea la view v_jobs_with_minutes che unisce trascrizione,
-- stato RAG e ultima minuta generata.
--
-- IDEMPOTENTE — sicuro da eseguire su fresh install e su upgrade.
--
-- Comando upgrade manuale su DB esistente:
--   docker exec -i n8n_stack_postgres psql -U n8n -d n8n \
--     < postgres_init/003_minutes_module.sql
--
-- Dipendenze:
--   001_create_transcription_runs.sql
--   002_transcription_extensions.sql
-- ============================================================


-- ── 1. ENUM minutes_template ─────────────────────────────────────────────────
-- Selezionato dall'utente nella UI minutes.html.
-- Determina il prompt template usato da minutes_worker.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'minutes_template') THEN
    CREATE TYPE minutes_template AS ENUM
      ('board', 'operativa', 'weekly', 'one_on_one');
  ELSE
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_template' AND e.enumlabel = 'board') THEN
      ALTER TYPE minutes_template ADD VALUE 'board';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_template' AND e.enumlabel = 'operativa') THEN
      ALTER TYPE minutes_template ADD VALUE 'operativa';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_template' AND e.enumlabel = 'weekly') THEN
      ALTER TYPE minutes_template ADD VALUE 'weekly';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_template' AND e.enumlabel = 'one_on_one') THEN
      ALTER TYPE minutes_template ADD VALUE 'one_on_one';
    END IF;
  END IF;
END$$;


-- ── 2. ENUM minutes_context_level ────────────────────────────────────────────
-- Determina il filtro Qdrant applicato dal rag_indexer durante il retrieval:
--   standalone      — nessun retrieval, solo trascrizione corrente
--   customer        — filter: {customer: X}
--   customer_project — filter: {customer: X, project: Y}
--   speakers        — filter: {speaker: any [lista speaker mappati]}
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'minutes_context_level') THEN
    CREATE TYPE minutes_context_level AS ENUM
      ('standalone', 'customer', 'customer_project', 'speakers');
  ELSE
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_context_level' AND e.enumlabel = 'standalone') THEN
      ALTER TYPE minutes_context_level ADD VALUE 'standalone';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_context_level' AND e.enumlabel = 'customer') THEN
      ALTER TYPE minutes_context_level ADD VALUE 'customer';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_context_level' AND e.enumlabel = 'customer_project') THEN
      ALTER TYPE minutes_context_level ADD VALUE 'customer_project';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                   WHERE t.typname = 'minutes_context_level' AND e.enumlabel = 'speakers') THEN
      ALTER TYPE minutes_context_level ADD VALUE 'speakers';
    END IF;
  END IF;
END$$;


-- ── 3. rag_index_jobs ────────────────────────────────────────────────────────
-- Traccia lo stato di indicizzazione di ogni trascrizione su Qdrant.
-- Flusso: wx_worker COMPLETED → rpush rag:queue → rag_indexer → upsert Qdrant → COMPLETED
-- Un job di trascrizione ha al massimo una riga (UNIQUE su job_id).
CREATE TABLE IF NOT EXISTS public.rag_index_jobs (
  id            serial               PRIMARY KEY,
  job_id        text                 NOT NULL
                  REFERENCES public.wx_transcription_jobs(job_id)
                  ON DELETE CASCADE,
  status        transcription_status NOT NULL DEFAULT 'PENDING',
  chunk_count   int                  DEFAULT 0,   -- numero di chunk indicizzati su Qdrant
  error_message text,
  created_at    timestamptz          NOT NULL DEFAULT now(),
  updated_at    timestamptz          NOT NULL DEFAULT now(),
  finished_at   timestamptz,

  CONSTRAINT rag_index_jobs_job_id_unique UNIQUE (job_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_index_jobs_status
  ON public.rag_index_jobs(status);
CREATE INDEX IF NOT EXISTS idx_rag_index_jobs_job_id
  ON public.rag_index_jobs(job_id);

DROP TRIGGER IF EXISTS trg_rag_index_jobs_updated_at ON public.rag_index_jobs;
CREATE TRIGGER trg_rag_index_jobs_updated_at
  BEFORE UPDATE ON public.rag_index_jobs
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


-- ── 4. minutes_jobs ──────────────────────────────────────────────────────────
-- Un job di generazione minuta è sempre associato a una trascrizione completata.
-- Creata da minutes_api.py (POST /minutes/jobs).
-- Consumata da minutes_worker.py (coda Redis minutes:queue).
-- Una trascrizione può avere N minute (retry, template diversi).
CREATE TABLE IF NOT EXISTS public.minutes_jobs (
  minutes_id      text                  PRIMARY KEY,  -- uuid hex generato da minutes_api
  job_id          text                  NOT NULL
                    REFERENCES public.wx_transcription_jobs(job_id)
                    ON DELETE CASCADE,
  status          transcription_status  NOT NULL DEFAULT 'PENDING',
  step            text,

  -- Parametri AI (scelti dall'utente nella UI)
  template        minutes_template      NOT NULL DEFAULT 'operativa',
  context_level   minutes_context_level NOT NULL DEFAULT 'standalone',
  model           text                  NOT NULL DEFAULT 'mistral:7b',
  language        text                  NOT NULL DEFAULT 'it',

  -- Metadati riunione (denormalizzati da wx_transcription_jobs per query veloci)
  customer        text                  NOT NULL DEFAULT '',
  project         text                  NOT NULL DEFAULT '',
  participants    text,                 -- JSON array [{speaker, name, role}]

  -- Output
  output_url      text,                 -- MinIO URL minuta .md (bucket: minutes)
  error_message   text,

  -- Timestamps
  created_at      timestamptz           NOT NULL DEFAULT now(),
  updated_at      timestamptz           NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz
);

CREATE INDEX IF NOT EXISTS idx_minutes_jobs_status
  ON public.minutes_jobs(status);
CREATE INDEX IF NOT EXISTS idx_minutes_jobs_job_id
  ON public.minutes_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_minutes_jobs_customer_project
  ON public.minutes_jobs(customer, project);
CREATE INDEX IF NOT EXISTS idx_minutes_jobs_created_at
  ON public.minutes_jobs(created_at DESC);

DROP TRIGGER IF EXISTS trg_minutes_jobs_updated_at ON public.minutes_jobs;
CREATE TRIGGER trg_minutes_jobs_updated_at
  BEFORE UPDATE ON public.minutes_jobs
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


-- ── 5. View v_jobs_with_minutes ───────────────────────────────────────────────
-- Snapshot completo: trascrizione + stato RAG + ultima minuta generata.
-- Usata da minutes.html e da n8n per monitoring.
-- Creata per ultima così tutte le colonne e tabelle esistono già.
CREATE OR REPLACE VIEW public.v_jobs_with_minutes AS
SELECT
  -- Trascrizione (da 001 + 002)
  j.job_id,
  j.customer,
  j.project,
  j.status            AS transcription_status,
  j.language,
  j.output_format,
  j.speakers_mapped,
  j.input_filename,
  j.output_json_url,
  j.output_txt_url,
  j.audio_url,
  j.created_at,
  j.finished_at,
  -- RAG indexing (da questo file, sezione 3)
  ri.status           AS rag_status,
  ri.chunk_count      AS rag_chunk_count,
  -- Ultima minuta generata (da questo file, sezione 4)
  mj.minutes_id,
  mj.status           AS minutes_status,
  mj.template         AS minutes_template,
  mj.context_level    AS minutes_context_level,
  mj.model            AS minutes_model,
  mj.output_url       AS minutes_url,
  mj.created_at       AS minutes_created_at,
  mj.finished_at      AS minutes_finished_at
FROM public.wx_transcription_jobs j
LEFT JOIN public.rag_index_jobs ri
  ON ri.job_id = j.job_id
LEFT JOIN public.minutes_jobs mj
  ON mj.minutes_id = (
    SELECT minutes_id
    FROM   public.minutes_jobs
    WHERE  job_id = j.job_id
    ORDER  BY created_at DESC
    LIMIT  1
  );


-- ── Verifica finale ───────────────────────────────────────────────────────────
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['rag_index_jobs', 'minutes_jobs'] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = t
    ) THEN
      RAISE EXCEPTION '003_minutes_module: tabella % mancante dopo migrazione', t;
    END IF;
  END LOOP;
  RAISE NOTICE '003_minutes_module: migrazione completata con successo.';
END$$;
