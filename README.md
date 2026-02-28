# n8n-stack (local/dev)

Repository per versionare i file necessari ad installare e configurare lo stack Docker in locale.

## Quick start

1. Prerequisiti:
   - Docker Desktop (Mac/Windows) oppure Docker Engine (Linux)
   - `docker compose` disponibile

2. Crea il file `.env` partendo da `.env.example`:
   ```bash
   cp .env.example .env
   # poi compila i valori (password, token, ecc.)
   ```

3. Avvio:
   ```bash
   docker compose up -d --build
   ```

4. Stop:
   ```bash
   docker compose down
   ```

## Cosa va in Git / cosa NO

✅ In Git:
- `docker-compose.yml`, `Dockerfile*`, `*.py`, `*.sql`, `webform/`, `nemo/`, `docs/`, `scripts/`
- `.env.example` (template, senza segreti)

❌ Non in Git (già esclusi da `.gitignore`):
- `.env` (segreti)
- directory volumi: `postgres_data/`, `redis_data/`, `qdrant_data/`, `minio_data/`, `ollama_data/`, `n8n_data/`
- cache / modelli: `whisper_cache/`, `hf_cache/`, `data/whisper_models/`, `data/nltk_data/`
- upload runtime: `data/uploads/`
- cartella `Backup/`

## Reset stato locale (cancella dati)

⚠️ Questo comando elimina i dati locali (DB, redis, minio, ecc.).

```bash
./scripts/reset_state.sh
```
