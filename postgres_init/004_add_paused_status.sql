-- ============================================================
-- 003_add_paused_status.sql
-- Aggiunge il valore PAUSED all'enum transcription_status
-- e la colonna paused_at alla tabella wx_transcription_jobs.
--
-- Idempotente: sicuro da rieseguire.
-- ============================================================

-- ── 1. Aggiunge PAUSED all'enum (se non esiste già) ──────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e
    JOIN pg_type t ON e.enumtypid = t.oid
    WHERE t.typname = 'transcription_status' AND e.enumlabel = 'PAUSED'
  ) THEN
    ALTER TYPE transcription_status ADD VALUE 'PAUSED';
  END IF;
END$$;

-- ── 2. Colonna paused_at ──────────────────────────────────────────────────────
ALTER TABLE public.wx_transcription_jobs
  ADD COLUMN IF NOT EXISTS paused_at timestamptz;

-- ── Comando rapido da terminale ───────────────────────────────────────────────
-- docker exec n8n_stack_postgres psql -U n8n -d n8n \
--   -c "ALTER TYPE transcription_status ADD VALUE IF NOT EXISTS 'PAUSED';" \
--   -c "ALTER TABLE public.wx_transcription_jobs ADD COLUMN IF NOT EXISTS paused_at timestamptz;"
