-- ============================================================
-- 004_add_deleted_status.sql
-- Aggiunge status DELETED all'enum e colonna audio_url
-- Idempotente.
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
    WHERE t.typname = 'transcription_status' AND e.enumlabel = 'DELETED'
  ) THEN
    ALTER TYPE transcription_status ADD VALUE 'DELETED';
  END IF;
END$$;

-- Colonna audio_url (potrebbe non esserci su installazioni precedenti)
ALTER TABLE public.wx_transcription_jobs
  ADD COLUMN IF NOT EXISTS audio_url text;
