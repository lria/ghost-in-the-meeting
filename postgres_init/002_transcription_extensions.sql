-- ============================================================
-- 002_transcription_extensions.sql
-- Estensioni alla pipeline di trascrizione WhisperX
--
-- Aggiunge a wx_transcription_jobs le colonne prodotte da
-- wx_worker e gestite da transcriber_api / wx_api.
-- Crea speaker_aliases per il mapping speaker → persona reale.
--
-- IDEMPOTENTE — sicuro da eseguire su fresh install e su upgrade.
--
-- Comando upgrade manuale su DB esistente:
--   docker exec -i n8n_stack_postgres psql -U n8n -d n8n \
--     < postgres_init/002_transcription_extensions.sql
--
-- Dipendenze: 001_create_transcription_runs.sql
-- ============================================================


-- ── 1. Funzione update_updated_at (idempotente) ───────────────────────────────
-- Ridichiarata con OR REPLACE per sicurezza su upgrade.
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


-- ── 2. Colonne aggiuntive su wx_transcription_jobs ───────────────────────────
-- Aggiunte rispetto allo schema base di 001:
--
--   output_txt_url  — URL MinIO del file TXT (scritto da wx_worker)
--   audio_url       — URL MinIO dell'audio originale (scritto da wx_worker)
--                     nota: già aggiunta da 004 su installazioni esistenti,
--                     ADD COLUMN IF NOT EXISTS è idempotente
--   output_format   — formato richiesto all'atto della submission: json | txt | both
--                     (scritto da wx_api, letto da wx_worker)
--   speakers_mapped — true quando l'utente ha assegnato nomi reali agli speaker
--                     (aggiornato da transcriber_api)
ALTER TABLE public.wx_transcription_jobs
  ADD COLUMN IF NOT EXISTS output_txt_url  text,
  ADD COLUMN IF NOT EXISTS audio_url       text,
  ADD COLUMN IF NOT EXISTS output_format   text NOT NULL DEFAULT 'both',
  ADD COLUMN IF NOT EXISTS speakers_mapped boolean NOT NULL DEFAULT false;


-- ── 3. speaker_aliases ───────────────────────────────────────────────────────
-- Mapping SPEAKER_00 → persona reale, per job.
--
-- Scritta da transcriber_api.py quando l'utente assegna i nomi nella UI.
-- Letta da minutes_worker.py per sostituire i label prima della generazione.
--
-- Un job può avere N speaker; ogni speaker ha un solo alias (UNIQUE job_id + speaker_id).
CREATE TABLE IF NOT EXISTS public.speaker_aliases (
  id           serial      PRIMARY KEY,
  job_id       text        NOT NULL
                             REFERENCES public.wx_transcription_jobs(job_id)
                             ON DELETE CASCADE,
  speaker_id   text        NOT NULL,   -- es. SPEAKER_00, UNKNOWN_0
  name         text        NOT NULL,   -- nome reale assegnato dall'utente
  company      text,                   -- azienda (opzionale)
  customer     text        NOT NULL,   -- denormalizzato per query per cliente
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


-- ── Verifica finale ───────────────────────────────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'speaker_aliases'
  ) THEN
    RAISE EXCEPTION '002_transcription_extensions: tabella speaker_aliases mancante';
  END IF;
  RAISE NOTICE '002_transcription_extensions: migrazione completata con successo.';
END$$;
