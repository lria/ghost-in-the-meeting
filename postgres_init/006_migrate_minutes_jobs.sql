-- ============================================================
-- 004_migrate_minutes_jobs.sql
-- Allinea minutes_jobs allo schema usato da minutes_api.py
--
-- Problema: la tabella era creata da transcriber_api (vecchio servizio)
-- con colonna "minutes_job_id". minutes_api usa "minutes_id".
-- Soluzione: aggiunge "minutes_id" come alias + colonne mancanti.
-- Idempotente: sicuro da rieseguire.
-- ============================================================

-- 1. Rinomina primary key da minutes_job_id → minutes_id (se esiste ancora)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='minutes_jobs' AND column_name='minutes_job_id'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='minutes_jobs' AND column_name='minutes_id'
  ) THEN
    ALTER TABLE minutes_jobs RENAME COLUMN minutes_job_id TO minutes_id;
  END IF;
END$$;

-- 2. Aggiungi colonne mancanti (idempotente con IF NOT EXISTS)
ALTER TABLE minutes_jobs
  ADD COLUMN IF NOT EXISTS step          text,
  ADD COLUMN IF NOT EXISTS context_level text NOT NULL DEFAULT 'standalone',
  ADD COLUMN IF NOT EXISTS language      text NOT NULL DEFAULT 'it',
  ADD COLUMN IF NOT EXISTS customer      text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS project       text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS participants  text;

-- 3. Ricrea primary key se necessario dopo il rename
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'minutes_jobs_pkey'
  ) THEN
    ALTER TABLE minutes_jobs ADD PRIMARY KEY (minutes_id);
  END IF;
END$$;

-- 4. Aggiorna foreign key constraint al nuovo nome colonna (se punta ancora a minutes_job_id)
-- (PostgreSQL aggiorna automaticamente i constraint al rename, quindi questa è solo una verifica)

-- 5. Ricrea indici utili
CREATE INDEX IF NOT EXISTS idx_minutes_jobs_minutes_id
  ON minutes_jobs(minutes_id);

CREATE INDEX IF NOT EXISTS idx_minutes_jobs_customer_project
  ON minutes_jobs(customer, project);

-- Verifica finale
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'minutes_jobs'
ORDER BY ordinal_position;
