# 👻 Ghost in the Meeting

Pipeline self-hosted e asincrona per trascrivere, diarizzare e trasformare in minuta le registrazioni audio di riunioni. Costruita su Apple Silicon (M3 Pro) con Docker. Funziona interamente in locale, senza cloud.

---

## Stack

| Servizio | Ruolo |
|---|---|
| **faster-whisper** | Speech-to-text (word timestamps + VAD) |
| **pyannote.audio** | Speaker diarization |
| **n8n** | Workflow orchestration (ingest webhook) |
| **PostgreSQL** | Job tracking e metadata persistente |
| **Redis** | Coda FIFO e stato dei job in tempo reale |
| **MinIO** | Storage oggetti (audio, JSON, TXT, minute .md) |
| **SSE Broker** | Push real-time eventi job al browser |
| **Qdrant** | Vector store per RAG sulle trascrizioni |
| **Ollama** | LLM locale per generazione minuta |
| **nginx** | Serve la Web UI statica |

---

## Porte

| Servizio | Porta | URL |
|---|---|---|
| Web UI | 8081 | http://localhost:8081 |
| n8n | 5678 | http://localhost:5678 |
| WhisperX API | 9200 | http://localhost:9200 |
| Transcriber API | 9500 | http://localhost:9500 |
| Minutes API | 9600 | http://localhost:9600 |
| SSE Broker | 9400 | http://localhost:9400 |
| MinIO API | 9000 | http://localhost:9000 |
| MinIO Console | 9001 | http://localhost:9001 |
| Ollama | 11434 | http://localhost:11434 |
| Qdrant REST | 6333 | http://localhost:6333 |

---

## Architettura completa

```
Browser (http://localhost:8081)
    │
    ├── index.html      — Upload audio + live status SSE + storico
    ├── speakers.html   — Associa SPEAKER_XX → nome reale
    ├── minutes.html    — Genera/visualizza minuta AI
    ├── history.html    — Storico trascrizioni dettagliato
    └── settings.html   — Configurazione
    │
    ▼ upload
n8n (5678) — POST /webhook/minute/ingest
    │         valida e forwarda a whisperx_api
    │
    ▼
whisperx_api (9200)          — FastAPI
    │  POST /jobs             → salva audio su disco, accoda su Redis
    │  GET  /jobs/:id         → polling stato + URL output
    │  POST /jobs/:id/pause   → setta stop_requested=1
    │  POST /jobs/:id/resume  → re-accoda job PAUSED
    │  POST /jobs/:id/retry   → re-accoda job FAILED
    │  DELETE /jobs/:id       → elimina risorse (4 scope)
    │  GET  /queue-stats      → snapshot coda
    │
    ▼ wx:queue (Redis FIFO)
    │
whisperx_worker
    ├── ffmpeg           → WAV 16kHz mono
    ├── faster-whisper   → trascrizione (word timestamps)
    ├── pyannote         → diarizzazione speaker (opzionale)
    ├── merge            → assegna speaker a ogni segmento
    └── MinIO upload     → result.json, result.txt, audio_original
         │
         ▼ rag:queue
    rag_indexer          → chunking + embedding (nomic-embed-text) + upsert Qdrant
    │
whisperx_cleanup (ogni ora) → elimina file temporanei su disco

sse_broker (9400)        — FastAPI
    └── poll Redis ogni 2s → push SSE al browser

transcriber_api (9500)   — FastAPI
    │  GET  /jobs/:id/speakers        → speaker rilevati + alias esistenti + suggerimenti
    │  POST /jobs/:id/speakers        → salva mapping speaker→nome in speaker_aliases
    │  GET  /jobs/:id/transcript-url  → URL pubblico del result.json su MinIO

minutes_api (9600)       — FastAPI
    │  POST /minutes/jobs             → crea job generazione minuta
    │  GET  /minutes/jobs/:id         → polling stato + preview
    │  GET  /minutes/jobs             → lista minute (filtro per job_id o customer)
    │  POST /minutes/jobs/:id/retry   → re-accoda job FAILED
    │  DELETE /minutes/jobs/:id       → elimina job da Redis e PG
    │  GET  /minutes/templates        → lista template disponibili
    │  GET  /minutes/models           → modelli Ollama disponibili
    │  POST /minutes/models/pull      → scarica un modello su Ollama
    │
    ▼ minutes:queue (Redis)
    │
minutes_worker
    ├── fetch_transcript  → scarica result.json da MinIO
    ├── speaker_aliases   → legge mapping da PG (sorgente di verità)
    ├── RAG retrieval     → Qdrant (se context_level != standalone)
    ├── build_prompt      → carica template + sostituisce variabili
    ├── Ollama generate   → streaming, aggiorna Redis ogni 100 token
    └── MinIO upload      → minuta .md nel bucket "minutes"
```

---

## Requisiti

- Docker Desktop for Mac (Apple Silicon M1/M2/M3)
- Docker Desktop RAM: almeno **10 GB** (Settings → Resources)
- Account HuggingFace con licenza accettata per:
  - [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1)
  - [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0)

---

## Setup completo — Prima installazione

### 1. Clone e configurazione

```bash
git clone https://github.com/lria/ghost-in-the-meeting
cd ghost-in-the-meeting
cp .env.example .env
```

Edita `.env` con i tuoi valori:

```env
POSTGRES_USER=n8n
POSTGRES_PASSWORD=una_password_sicura
POSTGRES_DB=n8n

MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=una_password_sicura

N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=una_password_sicura
N8N_ENCRYPTION_KEY=chiave_32_caratteri_esatti

HF_TOKEN=hf_il_tuo_token_huggingface
TZ=Europe/Rome
```

### 2. Avvio dello stack

```bash
docker compose up -d
```

Il primo avvio scarica i modelli Whisper e pyannote (~10 minuti). Verifica il progresso:

```bash
docker logs n8n_whisperx_worker --follow
```

Attendi il messaggio `[worker] Pipeline pyannote pronta.` prima di inviare il primo job.

### 3. Configurazione MinIO

Rendi il bucket pubblico in lettura (necessario per accedere ai file dal browser):

```bash
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD &&
  mc anonymous set download local/wx-transcriptions
'
```

Crea anche il bucket per le minute:

```bash
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD &&
  mc mb local/minutes --ignore-existing &&
  mc anonymous set download local/minutes
'
```

### 4. Migrazioni database

Le migrazioni sono eseguite automaticamente da Docker all'avvio tramite `postgres_init/`. Se aggiorni il progetto su un'installazione esistente, applica manualmente le migrazioni mancanti:

```bash
# Esegui tutte le migrazioni in ordine
for f in postgres_init/*.sql; do
  docker exec -i n8n_stack_postgres psql -U n8n -d n8n < "$f"
done
```

### 5. Installazione modelli Ollama

Almeno un modello è necessario per la generazione delle minute. Raccomandati:

```bash
# Modello consigliato — buon equilibrio qualità/velocità su M3
docker exec n8n_stack_ollama ollama pull mistral:7b

# Alternativa più leggera (più veloce, meno preciso)
docker exec n8n_stack_ollama ollama pull llama3.2:3b

# Alternativa più potente (richiede più RAM)
docker exec n8n_stack_ollama ollama pull llama3.1:8b

# Modello embedding per il RAG (obbligatorio se si usa il contesto storico)
docker exec n8n_stack_ollama ollama pull nomic-embed-text
```

Puoi anche scaricare modelli dalla Web UI: `minutes.html` → sezione "Modello LLM" → pulsante "Pull".

Verifica i modelli installati:
```bash
docker exec n8n_stack_ollama ollama list
```

### 6. Verifica che tutto sia attivo

```bash
# Stato di tutti i container
docker compose ps

# Health check dei servizi principali
curl http://localhost:9200/health   # whisperx_api
curl http://localhost:9400/health   # sse_broker
curl http://localhost:9500/health   # transcriber_api
curl http://localhost:9600/health   # minutes_api
```

---

## Strutture dati e relazioni

### PostgreSQL — Schema

#### `wx_transcription_jobs` — Job principale di trascrizione
```sql
job_id            text PRIMARY KEY           -- uuid hex
status            transcription_status        -- PENDING|WORKING|COMPLETED|FAILED|PAUSED|DELETED
step              text                        -- step corrente (es. DIARIZING_42pct)
customer          text                        -- nome cliente
project           text                        -- nome progetto
language          text DEFAULT 'it'           -- it | en | auto
max_speakers      int  DEFAULT 8
diarize           boolean DEFAULT true
output_format     text DEFAULT 'both'         -- json | txt | both
input_filename    text
output_json_url   text                        -- URL MinIO result.json
output_txt_url    text                        -- URL MinIO result.txt
audio_url         text                        -- URL MinIO audio originale
speakers_mapped   boolean DEFAULT false       -- true dopo che l'utente ha assegnato i nomi
error_message     text
created_at        timestamptz
updated_at        timestamptz
started_at        timestamptz
finished_at       timestamptz
```

#### `speaker_aliases` — Mapping SPEAKER_XX → persona reale
```sql
id          serial PRIMARY KEY
job_id      text REFERENCES wx_transcription_jobs(job_id) ON DELETE CASCADE
speaker_id  text   -- es. SPEAKER_00, SPEAKER_01, UNKNOWN_0
name        text   -- nome reale assegnato dall'utente
company     text   -- azienda (opzionale)
customer    text   -- denormalizzato per query per cliente
created_at  timestamptz
updated_at  timestamptz
UNIQUE (job_id, speaker_id)
```

#### `rag_index_jobs` — Tracciamento indicizzazione Qdrant
```sql
id          serial PRIMARY KEY
job_id      text REFERENCES wx_transcription_jobs(job_id) ON DELETE CASCADE UNIQUE
status      transcription_status  -- PENDING|WORKING|COMPLETED|FAILED
chunk_count int   -- numero di chunk indicizzati
error_message text
created_at  timestamptz
updated_at  timestamptz
finished_at timestamptz
```

#### `minutes_jobs` — Job di generazione minuta
```sql
minutes_id    text PRIMARY KEY   -- uuid hex
job_id        text REFERENCES wx_transcription_jobs(job_id) ON DELETE CASCADE
status        transcription_status
step          text
template      text               -- chiave del file template (es. standard)
context_level text               -- standalone|customer|customer_project|speakers
model         text               -- nome modello Ollama (es. mistral:7b)
language      text DEFAULT 'it'
customer      text               -- denormalizzato da wx_transcription_jobs
project       text               -- denormalizzato da wx_transcription_jobs
participants  text               -- JSON [{speaker, name, role}] — backup; PG è sorgente di verità
output_url    text               -- URL MinIO file .md nel bucket "minutes"
error_message text
created_at    timestamptz
updated_at    timestamptz
started_at    timestamptz
finished_at   timestamptz
```

### Relazioni tra tabelle

```
wx_transcription_jobs (1)
    │
    ├── (N) speaker_aliases        job_id FK → CASCADE DELETE
    ├── (1) rag_index_jobs         job_id FK → CASCADE DELETE, UNIQUE
    └── (N) minutes_jobs           job_id FK → CASCADE DELETE
```

### Redis — Strutture chiave

**Job trascrizione** → hash `wx:job:{job_id}`
```
status          PENDING|WORKING|COMPLETED|FAILED|PAUSED|DELETED
step            step corrente
customer        nome cliente
project         nome progetto
language        it|en|auto
diarize         true|false
max_speakers    numero intero
output_format   json|txt|both
output_json_url URL MinIO result.json
output_txt_url  URL MinIO result.txt
audio_url       URL MinIO audio originale
minutes_url     URL MinIO ultima minuta generata
input_file      path locale del file audio
stop_requested  0|1  (setta a 1 per pausare il worker)
created_at      ISO timestamp
updated_at      ISO timestamp
```

**Job minuta** → hash `minutes:job:{minutes_id}`
```
minutes_id      uuid hex
job_id          riferimento al job trascrizione
status          PENDING|WORKING|COMPLETED|FAILED
step            step corrente (es. GENERATING_350tok)
template        chiave template
context_level   standalone|customer|customer_project|speakers
model           nome modello Ollama
language        it|en
customer        nome cliente
project         nome progetto
participants    JSON array [{speaker, name, role}]
output_url      URL MinIO file .md
preview         primi 8000 chars della minuta (per preview rapida)
created_at      ISO timestamp
updated_at      ISO timestamp
```

**Code Redis:**
```
wx:queue        → lista job_id in attesa di trascrizione (FIFO)
rag:queue       → lista job_id in attesa di indicizzazione Qdrant
minutes:queue   → lista minutes_id in attesa di generazione
```

### MinIO — Struttura bucket

**Bucket `wx-transcriptions`** — accesso pubblico read
```
{customer}/{project}/{YYYYMMDD_HHMMSS}_{job_id[:8]}/
    ├── result.json          — trascrizione strutturata + speaker
    ├── result.txt           — trascrizione leggibile con speaker e timestamp
    └── audio_original.{ext} — file audio originale
```

**Bucket `minutes`** — accesso pubblico read
```
{customer}/{project}/{YYYY-MM-DD}_{job_id[:8]}/
    └── {customer} - {project} - {YYYY-MM-DD}.md   — minuta generata
```

### Qdrant — Collection `transcriptions`

Ogni punto rappresenta un chunk della trascrizione:
```json
{
  "id": "<uint64>",
  "vector": "<nomic-embed-text 768d>",
  "payload": {
    "job_id":      "...",
    "customer":    "Acme",
    "project":     "Q1Review",
    "date":        "2026-03-18",
    "chunk_index": 0,
    "speaker":     "Luigi Ria",
    "text":        "chunk testo..."
  }
}
```

---

## Use case completo: da audio a minuta

### Fase 1 — Upload e trascrizione

1. L'utente apre `http://localhost:8081`, trascina il file audio nella Web UI
2. Compila: **Cliente**, **Progetto**, **Lingua**, **Max Speaker**, **Formato output**, toggle **Diarizzazione**
3. Clicca **Trascrivi** — il form invia `POST` a n8n webhook, che forwarda a `whisperx_api`
4. `whisperx_api` salva il file audio in `data/uploads/in/{job_id}/` e accoda il `job_id` su `wx:queue`
5. `sse_broker` riporta in tempo reale l'aggiornamento al browser via SSE
6. `whisperx_worker` prende il job dalla coda:
   - ffmpeg converte l'audio in WAV 16kHz mono
   - faster-whisper trascrive con word timestamps
   - pyannote esegue la diarizzazione (se abilitata)
   - merge assegna `SPEAKER_XX` a ogni segmento
   - Carica `result.json`, `result.txt` e l'audio originale su MinIO
   - Accoda il `job_id` su `rag:queue`
7. `rag_indexer` chunka la trascrizione, crea embedding con nomic-embed-text e fa upsert su Qdrant

### Fase 2 — Associazione speaker

1. L'utente clicca **Associa Speaker** nella Web UI → `speakers.html?job_id=...`
2. La pagina chiama `GET /jobs/{id}/speakers` su `transcriber_api` (porta 9500):
   - Scarica `result.json` da MinIO e estrae la lista degli speaker unici
   - Legge gli alias già salvati in `speaker_aliases` (se presenti)
   - Recupera suggerimenti da job precedenti dello stesso cliente
3. L'utente inserisce nome e azienda per ogni `SPEAKER_XX`
4. Clicca **Salva** → `POST /jobs/{id}/speakers` salva in `speaker_aliases` su PG e setta `speakers_mapped=true`

### Fase 3 — Generazione minuta

1. L'utente clicca **Genera Minuta** → `minutes.html?job_id=...`
2. Sceglie:
   - **Template**: tipo di documento (Verbale Standard, Minuta Dettagliata, 1:1)
   - **Modello LLM**: quale modello Ollama usare
   - **Contesto RAG**: quanto contesto storico includere
3. Clicca **Genera** → `POST /minutes/jobs` su `minutes_api` (porta 9600)
4. `minutes_worker` prende il job da `minutes:queue`:
   - Scarica `result.json` da MinIO (con `_parse_minio_url` che gestisce gli URL pubblici)
   - Legge gli alias da `speaker_aliases` in PG (sorgente di verità prioritaria)
   - Sostituisce `SPEAKER_XX` con i nomi reali nella trascrizione
   - Se `context_level != standalone`: fa retrieval su Qdrant con filtro per cliente/progetto/speaker
   - Carica il template `.txt`, sostituisce le variabili
   - Chiama Ollama in streaming, aggiorna Redis ogni 100 token
   - Carica la minuta `.md` sul bucket `minutes` di MinIO
   - Aggiorna Redis e PG con `status=COMPLETED` e l'URL della minuta
5. La Web UI mostra la minuta in anteprima Markdown formattata

---

## API Reference

### WhisperX API — `http://localhost:9200`

#### `POST /jobs` — Invia un job di trascrizione

```bash
curl -X POST http://localhost:9200/jobs \
  -F "file=@meeting.mp3" \
  -F "customer=Acme" \
  -F "project=Q1Review" \
  -F "language=it" \
  -F "diarize=true" \
  -F "output_format=both" \
  -F "max_speakers=6"
```

| Parametro | Tipo | Default | Descrizione |
|---|---|---|---|
| `file` | File | richiesto | File audio (mp3, m4a, wav, mp4, …) |
| `customer` | str | richiesto | Nome cliente — usato come namespace MinIO |
| `project` | str | richiesto | Nome progetto — usato come namespace MinIO |
| `language` | str | `it` | Codice lingua: `it`, `en`, `auto` |
| `max_speakers` | int | `8` | Max speaker (1–20) |
| `diarize` | bool | `true` | Abilita diarizzazione |
| `output_format` | str | `both` | `json` \| `txt` \| `both` |
| `participants` | str | `""` | Lista partecipanti (riservato) |

Risposta `202`:
```json
{
  "job_id": "d87b69740f634fd28ad1d5dda935e963",
  "status": "PENDING",
  "customer": "Acme",
  "project": "Q1Review",
  "output_format": "both"
}
```

#### `GET /jobs/{job_id}` — Polling stato

```bash
curl http://localhost:9200/jobs/d87b6974...
```

Risposta durante elaborazione:
```json
{ "status": "WORKING", "step": "DIARIZING_EMBEDDINGS_42pct" }
```

Risposta completato:
```json
{
  "status": "COMPLETED",
  "output_json_url": "http://localhost:9000/wx-transcriptions/Acme/Q1Review/20260318_153045_d87b6974/result.json",
  "output_txt_url":  "http://localhost:9000/wx-transcriptions/Acme/Q1Review/20260318_153045_d87b6974/result.txt",
  "audio_url":       "http://localhost:9000/wx-transcriptions/Acme/Q1Review/20260318_153045_d87b6974/audio_original.mp3",
  "output_format": "both",
  "finished_at": "2026-03-18T15:30:45Z"
}
```

#### `POST /jobs/{job_id}/pause` — Sospendi

```bash
curl -X POST http://localhost:9200/jobs/d87b6974.../pause
```

Risposta:
```json
{ "job_id": "...", "stop_requested": true, "current_status": "WORKING" }
```

#### `POST /jobs/{job_id}/resume` — Riprendi

```bash
curl -X POST http://localhost:9200/jobs/d87b6974.../resume
```

Risposta 200: `{ "job_id": "...", "status": "PENDING", "step": "RESUMED" }`

Risposta 410 (audio rimosso): `{ "error": "audio_missing", "detail": "..." }`

#### `POST /jobs/{job_id}/retry` — Riprova job FAILED

```bash
curl -X POST http://localhost:9200/jobs/d87b6974.../retry
```

#### `DELETE /jobs/{job_id}?scope=` — Elimina risorse

```bash
# Elimina solo l'audio
curl -X DELETE "http://localhost:9200/jobs/d87b6974...?scope=audio"

# Elimina audio + trascrizione (soft delete)
curl -X DELETE "http://localhost:9200/jobs/d87b6974...?scope=transcript"

# Elimina tutto incluso RAG → status=DELETED
curl -X DELETE "http://localhost:9200/jobs/d87b6974...?scope=rag"

# Rimuove ogni traccia (solo su job già DELETED)
curl -X DELETE "http://localhost:9200/jobs/d87b6974...?scope=purge"
```

Risposta:
```json
{
  "job_id": "...",
  "scope": "rag",
  "ops": [
    "MinIO deleted: wx-transcriptions/Acme/...",
    "Qdrant deleted points for job_id=...",
    "PG deleted: speaker_aliases, minutes_jobs, rag_index_jobs",
    "status → DELETED"
  ]
}
```

#### `GET /queue-stats` — Snapshot coda

```bash
curl http://localhost:9200/queue-stats
```

```json
{
  "queue_length": 1,
  "queued_ids": ["abc123"],
  "status_counts": { "PENDING": 1, "WORKING": 0, "COMPLETED": 5, "FAILED": 0 },
  "stale_working": []
}
```

---

### Transcriber API — `http://localhost:9500`

#### `GET /jobs/{job_id}/speakers` — Speaker rilevati + alias

```bash
curl http://localhost:9500/jobs/d87b6974.../speakers
```

```json
{
  "job_id": "...",
  "customer": "Acme",
  "project": "Q1Review",
  "speakers": [
    {
      "speaker_id": "SPEAKER_00",
      "segments": 42,
      "duration_sec": 185.3,
      "mapped": true,
      "name": "Luigi Ria",
      "company": "Kiratech"
    }
  ],
  "suggestions": [
    { "speaker_id": "SPEAKER_00", "name": "Luigi Ria", "company": "Kiratech", "last_used": "2026-03-10T..." }
  ],
  "speakers_mapped": true
}
```

#### `POST /jobs/{job_id}/speakers` — Salva mapping speaker

```bash
curl -X POST http://localhost:9500/jobs/d87b6974.../speakers \
  -H "Content-Type: application/json" \
  -d '{
    "aliases": [
      { "speaker_id": "SPEAKER_00", "name": "Luigi Ria", "company": "Kiratech" },
      { "speaker_id": "SPEAKER_01", "name": "Mario Rossi", "company": "Acme" }
    ]
  }'
```

```json
{ "ok": true, "job_id": "...", "aliases_saved": 2 }
```

#### `GET /jobs/{job_id}/transcript-url` — URL pubblico trascrizione

```bash
curl http://localhost:9500/jobs/d87b6974.../transcript-url
```

```json
{ "job_id": "...", "url": "http://localhost:9000/wx-transcriptions/Acme/.../result.json" }
```

---

### Minutes API — `http://localhost:9600`

#### `POST /minutes/jobs` — Crea job generazione minuta

```bash
curl -X POST http://localhost:9600/minutes/jobs \
  -F "job_id=d87b6974..." \
  -F "template=standard" \
  -F "context_level=customer_project" \
  -F "model=mistral:7b" \
  -F "participants=[]"
```

| Parametro | Tipo | Default | Descrizione |
|---|---|---|---|
| `job_id` | str | richiesto | ID del job di trascrizione |
| `template` | str | `standard` | Chiave del file template |
| `context_level` | str | `standalone` | `standalone` \| `customer` \| `customer_project` \| `speakers` |
| `model` | str | richiesto | Nome modello Ollama |
| `participants` | str | `[]` | JSON array (il worker usa PG come sorgente primaria) |

Risposta `202`:
```json
{
  "minutes_id": "ac1f6432c600462ea9afa45c26efb940",
  "status": "PENDING",
  "customer": "Acme",
  "project": "Q1Review",
  "template": "standard",
  "context_level": "customer_project",
  "model": "mistral:7b"
}
```

Risposta `409` (job già attivo per lo stesso template):
```json
{
  "detail": {
    "error": "duplicate_active",
    "minutes_id": "...",
    "message": "Esiste già un job attivo per template 'standard'"
  }
}
```

#### `GET /minutes/jobs/{minutes_id}` — Polling stato

```bash
curl http://localhost:9600/minutes/jobs/ac1f6432...
```

```json
{
  "minutes_id": "ac1f6432...",
  "job_id": "d87b6974...",
  "status": "COMPLETED",
  "step": "DONE",
  "template": "standard",
  "context_level": "customer_project",
  "model": "mistral:7b",
  "output_url": "http://localhost:9000/minutes/Acme/Q1Review/2026-03-18_d87b6974/Acme - Q1Review - 2026-03-18.md",
  "preview": "# Verbale Riunione\n\n## ...",
  "finished_at": "2026-03-18T16:00:00Z"
}
```

#### `GET /minutes/jobs` — Lista minute

```bash
# Tutte le minute di un job
curl "http://localhost:9600/minutes/jobs?job_id=d87b6974..."

# Tutte le minute di un cliente
curl "http://localhost:9600/minutes/jobs?customer=Acme&limit=20"
```

#### `DELETE /minutes/jobs/{minutes_id}` — Elimina job minuta

```bash
curl -X DELETE http://localhost:9600/minutes/jobs/ac1f6432...
```

```json
{ "minutes_id": "ac1f6432...", "deleted": true }
```

#### `GET /minutes/templates` — Template disponibili

```bash
curl http://localhost:9600/minutes/templates
```

```json
{
  "templates": {
    "standard": {
      "key": "standard",
      "name": "Verbale Standard",
      "tags": ["verbale", "formale", "decisioni", "azioni"],
      "description": "Verbale formale con sezioni fisse: oggetto, punti discussi, decisioni, azioni, prossimi passi"
    }
  }
}
```

#### `GET /minutes/models` — Modelli Ollama

```bash
curl http://localhost:9600/minutes/models
```

```json
{
  "installed": [
    { "name": "mistral:7b", "size_gb": 4.1 }
  ],
  "suggested": [
    { "name": "mistral:7b", "description": "Buon equilibrio qualità/velocità" }
  ]
}
```

#### `POST /minutes/models/pull` — Scarica modello

```bash
curl -X POST http://localhost:9600/minutes/models/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.2:3b"}'
```

```json
{ "status": "pulling", "model": "llama3.2:3b" }
```

---

### SSE Broker — `http://localhost:9400`

#### `GET /jobs` — Snapshot tutti i job

```bash
curl http://localhost:9400/jobs
```

#### `GET /events?all=1` — Stream in tempo reale (tutti i job)

```javascript
const es = new EventSource('http://localhost:9400/events?all=1');
es.addEventListener('job_update', e => {
  const job = JSON.parse(e.data);
  console.log(job.job_id, job.status, job.step);
});
```

#### `GET /events?jobs=id1,id2` — Stream filtrato per job_id

---

## Gestione delle minute

### Template

I template sono file `.txt` in `./minutes/templates/`. Aggiungere un template = creare un file, zero codice.

**Formato:**
```
Title: Nome del template
Tags: tag1, tag2, tag3
Description: Descrizione visualizzata nel tooltip della UI
---
<testo del prompt con variabili {{ var }}>
```

**Variabili disponibili nel prompt:**
```
{{ customer }}        — nome cliente
{{ project }}         — nome progetto
{{ date }}            — data della riunione (YYYY-MM-DD)
{{ participants }}    — lista partecipanti (nome + azienda)
{{ duration }}        — durata stimata
{{ transcript }}      — trascrizione annotata con speaker
{{ rag_context }}     — contesto RAG da riunioni precedenti (vuoto se standalone)
{{ rag_chunks_used }} — numero di chunk RAG utilizzati
```

**Blocchi condizionali:**
```
{% if rag_context %}
## Contesto da riunioni precedenti
{{ rag_context }}
{% endif %}
```

I template sono ricaricati al riavvio di `minutes_api` e `minutes_worker`. Per ricaricarli senza restart:

```bash
docker restart n8n_minutes_api n8n_minutes_worker
```

### Livelli di contesto RAG

| Livello | Descrizione | Filtro Qdrant |
|---|---|---|
| `standalone` | Nessun contesto storico | Nessuno — solo la trascrizione corrente |
| `customer` | Tutte le riunioni dello stesso cliente | `customer == "Acme"` |
| `customer_project` | Riunioni precedenti dello stesso progetto | `customer == "Acme" AND project == "Q1Review"` |
| `speakers` | Interventi storici degli stessi partecipanti | `speaker IN [nomi mappati]` |

Il job corrente è sempre escluso dal retrieval (`must_not: job_id`).

### Flusso speaker → minuta

```
speaker_aliases (PG)          ← sorgente di verità
    │
    ├── letta da minutes_worker al momento della generazione
    ├── usata per rinominare SPEAKER_XX → nome reale nella trascrizione
    ├── usata per costruire la lista partecipanti nel prompt
    └── usata per il filtro RAG context_level=speakers
```

**Priorità nel worker:**
1. `speaker_aliases` da PG (sempre letto) → usato se presente
2. `participants` da Redis (passato dalla UI) → fallback se PG è vuoto
3. Nessun mapping → log warning, usa `SPEAKER_XX` nel prompt

---

## Gestione storico e controlli

### Stati di un job

```
PENDING   → in attesa nella coda wx:queue
WORKING   → worker in elaborazione
COMPLETED → trascrizione completata, file su MinIO
FAILED    → errore — consultare step e error_message
PAUSED    → sospeso (stop_requested=1) — file audio preservato su disco
DELETED   → soft delete — metadata intatti, file eliminati
```

### Operazioni di eliminazione

Il DELETE su `wx_api` supporta 4 scope progressivi. Ogni scope include quelli precedenti:

```
audio      → MinIO audio_url + data/in/{job_id}/
transcript → audio + MinIO result.json + result.txt + data/out/{job_id}/
rag        → transcript + Qdrant points + speaker_aliases + minutes_jobs
             + rag_index_jobs + file .md dal bucket minutes
             → status = DELETED (riga PG intatta)
purge      → (solo su DELETED) Redis key + DELETE FROM wx_transcription_jobs (cascade)
```

### Recovery automatico

Al riavvio del worker, `recover_stale_jobs()` ri-accoda:
- Job in `PENDING` con file audio ancora su disco
- Job in `WORKING` da più di 45 minuti (stale: worker crashato)

I job `PAUSED` non vengono mai ri-accodati automaticamente.

### Cleanup automatico

`whisperx_cleanup` gira ogni ora e rimuove `data/in/{job_id}/` e `data/out/{job_id}/` per i job in stato `COMPLETED` o `FAILED` (mai i `PAUSED`).

Configurabile via env:
- `WX_CLEANUP_INTERVAL_SEC` — intervallo tra i cicli (default: 3600)
- `WX_CLEANUP_MIN_AGE_SEC` — età minima del job prima di pulire (default: 300)

### Comandi utili

```bash
# Log in tempo reale del worker
docker logs n8n_whisperx_worker --follow
docker logs n8n_minutes_worker --follow

# Stato della coda
docker exec n8n_stack_redis redis-cli LLEN wx:queue
docker exec n8n_stack_redis redis-cli LLEN minutes:queue

# Ispezione di un job
docker exec n8n_stack_redis redis-cli HGETALL wx:job:JOB_ID
docker exec n8n_stack_redis redis-cli HGETALL minutes:job:MINUTES_ID

# Speaker aliases di un job
docker exec n8n_stack_postgres psql -U n8n -d n8n -c \
  "SELECT speaker_id, name, company FROM speaker_aliases WHERE job_id = 'JOB_ID';"

# File su MinIO
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD > /dev/null &&
  mc ls --recursive local/wx-transcriptions
'

# Reset completo dati di test (mantiene modelli e configurazione)
./scripts/reset.sh

# Reset con anche Qdrant
./scripts/reset.sh --qdrant
```

---

## Performance (Apple M3 Pro, solo CPU)

| Durata audio | Trascrizione | Diarizzazione | Totale |
|---|---|---|---|
| 72 min | ~18 min | ~35 min | ~55 min |

- Modello: `small`, compute type: `int8`
- Docker Desktop: 12 CPU, 16 GB RAM
- GPU/Neural Engine non disponibili dentro Docker su macOS (limitazione Hypervisor)

---

## Roadmap

- [x] Pipeline trascrizione asincrona (faster-whisper + pyannote)
- [x] Speaker diarization con progresso real-time
- [x] Output doppio formato (JSON + TXT)
- [x] Job queue con stale recovery
- [x] Cleanup worker per file temporanei
- [x] MinIO folder naming human-readable
- [x] SSE broker per aggiornamenti real-time al browser
- [x] Web UI con stato live e pipeline steps
- [x] Pause / Resume job
- [x] Delete risorse job (4 scope: audio / transcript / rag / purge)
- [x] DELETED status — soft delete con metadata retention
- [x] Speaker rename (SPEAKER_XX → nome reale)
- [x] RAG indexing delle trascrizioni su Qdrant
- [x] Generazione minuta via Ollama (template system)
- [x] Contesto RAG multi-livello (standalone / customer / project / speakers)
- [x] Storico trascrizioni con viewer integrato
- [x] Conferma eliminazione inline nel modale storia 
- [ ] n8n workflow per automazione end-to-end
- [ ] Integrazione delle notifiche del broker SSE verso browser
- [ ] Export minuta in formato DOCX/PDF