# 👻 Ghost in the Meeting

A self-hosted, async pipeline for transcribing and diarizing meeting audio, built on Apple Silicon (M3 Pro) with Docker.

Transcribes audio in Italian and English, identifies speakers, and produces structured output ready for minute generation via RAG + LLM.

---

## Stack

| Service | Role |
|---|---|
| **faster-whisper** | Speech-to-text transcription |
| **pyannote.audio** | Speaker diarization |
| **n8n** | Workflow orchestration |
| **PostgreSQL** | Job tracking and metadata |
| **Redis** | Job queue (FIFO) and job state |
| **MinIO** | Output storage (JSON + TXT) |
| **SSE Broker** | Real-time job events to the browser |
| **Qdrant** | Vector store (RAG, upcoming) |
| **Ollama** | Local LLM for minute generation (upcoming) |

---

## Architecture

```
User
    │
    ▼
webform (nginx — port 8081)
    │  drag-and-drop upload, live status via SSE
    │
    ▼
n8n (port 5678)
    │  POST /webhook/minute/ingest
    │  validates + forwards to whisperx_api
    │
    ▼
whisperx_api          (FastAPI — port 9200)
    │  POST /jobs           → saves audio to disk, enqueues job on Redis
    │  GET  /jobs/:id       → polls status + returns output URLs
    │  POST /jobs/:id/pause → suspends job (worker skips it on dequeue)
    │  POST /jobs/:id/resume→ re-enqueues a paused job
    │  DELETE /jobs/:id     → deletes job resources (4 scopes)
    │
    ▼ Redis queue (wx:queue)
    │
    ▼
whisperx_worker
    ├── ffmpeg          → convert to WAV 16kHz mono
    ├── faster-whisper  → transcribe (word timestamps + VAD)
    ├── pyannote        → speaker diarization (optional)
    ├── merge           → assign speaker label to each segment
    └── MinIO upload    → result.json and/or result.txt

whisperx_cleanup       (runs every hour)
    └── deletes local temp files for COMPLETED/FAILED jobs
        never touches PAUSED jobs (audio needed for resume)

sse_broker             (FastAPI — port 9400)
    └── polls Redis every 2s → pushes job deltas to browser via SSE
```

---

## Requirements

- Docker Desktop for Mac (Apple Silicon M1/M2/M3)
- Docker Desktop RAM: at least 10 GB (Settings → Resources)
- HuggingFace account with accepted license for:
  - [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1)
  - [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0)

---

## Setup

**1. Clone and configure**
```bash
git clone https://github.com/lria/ghost-in-the-meeting
cd ghost-in-the-meeting
cp .env.example .env
# edit .env with your values
```

**2. Required `.env` variables**
```env
POSTGRES_USER=n8n
POSTGRES_PASSWORD=your_password
POSTGRES_DB=n8n

MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=your_password

N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=your_password
N8N_ENCRYPTION_KEY=your_32char_key

HF_TOKEN=hf_your_huggingface_token
TZ=Europe/Rome
```

**3. Start the stack**
```bash
docker compose up -d
```

**4. Run database migrations**
```bash
docker exec -i n8n_stack_postgres psql -U n8n -d n8n < postgres_init/003_add_paused_status.sql
docker exec -i n8n_stack_postgres psql -U n8n -d n8n < postgres_init/004_add_deleted_status.sql
```

**5. Make MinIO bucket publicly readable**
```bash
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD &&
  mc anonymous set download local/wx-transcriptions
'
```

**6. First run** — build takes ~10 minutes (downloads PyTorch + pyannote models)

---

## Web UI

Open `http://localhost:8081` in your browser.

**Left column — Nuova Trascrizione:** drag-and-drop audio upload, customer/project metadata, language, speaker count, output format, diarization toggle.

**Right column — Stato Trascrizioni:**
- **Trascrizione Corrente**: live progress bar with %, animated pipeline steps, **Sospendi** and **Elimina** buttons
- **Storico Trascrizioni**: accordion list of completed/failed/deleted jobs with download links, inline actions (Associa Speaker, Genera Minuta, Riprova, Elimina)

---

## API

Base URL: `http://localhost:9200`

### `POST /jobs` — Submit a transcription job

All parameters are `Form` fields (multipart body).

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

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file` | File | required | Audio file (mp3, m4a, wav, mp4…) |
| `customer` | str | required | Customer name — used as MinIO path namespace |
| `project` | str | required | Project name — used as MinIO path namespace |
| `language` | str | `it` | Language code: `it`, `en`, `auto` |
| `max_speakers` | int | `8` | Max number of speakers (1–20) |
| `diarize` | bool | `true` | Enable speaker diarization |
| `output_format` | str | `both` | Output format: `json` \| `txt` \| `both` |
| `participants` | str | `""` | Participant list (reserved for speaker rename) |

**Response `202`:**
```json
{
  "job_id": "d87b69740f634fd28ad1d5dda935e963",
  "status": "PENDING",
  "customer": "Acme",
  "project": "Q1Review",
  "output_format": "both"
}
```

---

### `GET /jobs/{job_id}` — Poll job status

```bash
curl http://localhost:9200/jobs/d87b69740f634fd28ad1d5dda935e963
```

**While processing:**
```json
{ "status": "WORKING", "step": "DIARIZING_EMBEDDINGS_42pct" }
```

**When completed:**
```json
{
  "status": "COMPLETED",
  "output_json_url": "http://localhost:9000/wx-transcriptions/Acme/Q1Review/20260307_173045_d87b6974/result.json",
  "output_txt_url":  "http://localhost:9000/wx-transcriptions/Acme/Q1Review/20260307_173045_d87b6974/result.txt",
  "output_format": "both",
  "finished_at": "2026-03-07T17:30:00Z"
}
```

---

### `POST /jobs/{job_id}/pause` — Suspend a job

```bash
curl -X POST http://localhost:9200/jobs/d87b69740f634fd28ad1d5dda935e963/pause
```

Sets `status=PAUSED`. The worker skips paused jobs on dequeue. The audio file is preserved on disk for resume.

---

### `POST /jobs/{job_id}/resume` — Resume a paused job

```bash
curl -X POST http://localhost:9200/jobs/d87b69740f634fd28ad1d5dda935e963/resume
```

Sets `status=PENDING` and re-enqueues the job. Returns `410` if the audio file was already cleaned up.

---

### `DELETE /jobs/{job_id}?scope=` — Delete job resources

```bash
curl -X DELETE "http://localhost:9200/jobs/d87b69740f634fd28ad1d5dda935e963?scope=transcript"
```

| scope | What it deletes |
|---|---|
| `audio` | Audio file from MinIO + local disk |
| `transcript` | Audio + JSON/TXT from MinIO |
| `rag` | Transcript + Qdrant chunks + `speaker_aliases` + `minutes_jobs` → sets `status=DELETED` |
| `purge` | Removes job completely from Redis + PostgreSQL (only on `DELETED` jobs) |

---

### `POST /jobs/{job_id}/retry` — Retry a failed job

```bash
curl -X POST http://localhost:9200/jobs/d87b69740f634fd28ad1d5dda935e963/retry
```

---

### `GET /queue-stats` — Queue snapshot

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

### `GET /health`

```bash
curl http://localhost:9200/health
```

---

## SSE Broker API

Base URL: `http://localhost:9400`

### `GET /jobs` — All jobs snapshot
Returns the full list of jobs from Redis. Used by the UI on page load.

### `GET /events?all=1` — Live stream (all jobs)
Server-Sent Events stream. Sends `job_update` events for any job state change.

### `GET /events?jobs=id1,id2` — Live stream (filtered)
SSE stream filtered to specific job IDs.

---

## Output formats

### JSON (`result.json`)
Structured output with word-level timestamps and speaker labels per segment.

```json
{
  "job_id": "...",
  "customer": "Acme",
  "project": "Q1Review",
  "language": "it",
  "speakers": ["SPEAKER_00", "SPEAKER_01"],
  "segments": [
    {
      "start": 5.12,
      "end": 9.84,
      "speaker": "SPEAKER_00",
      "text": "Buongiorno a tutti.",
      "words": [{ "start": 5.12, "end": 5.6, "word": "Buongiorno", "probability": 0.98 }]
    }
  ],
  "full_text": "Buongiorno a tutti. ..."
}
```

### TXT (`result.txt`)
Human-readable transcript grouped by speaker turn, with timestamps.
When diarization is disabled, blocks are split on silence gaps > 2 seconds.

```
RIUNIONE:  Acme / Q1Review
DATA:      2026-03-07
LINGUA:    it
SPEAKER:   SPEAKER_00, SPEAKER_01

────────────────────────────────────────────────────────────

[00:00:05] SPEAKER_00
Buongiorno a tutti, iniziamo la riunione.

[00:00:42] SPEAKER_01
Grazie. Come dicevo la settimana scorsa...

────────────────────────────────────────────────────────────

TESTO COMPLETO:
Buongiorno a tutti, iniziamo la riunione. Grazie. Come dicevo...
```

---

## Cleanup worker

The `whisperx_cleanup` service runs periodically and deletes temporary files
on disk (input audio + local output) for jobs already uploaded to MinIO.

- **`WORKING` / `PENDING` / `PAUSED` jobs are never touched**, regardless of age
- Configurable via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `WX_CLEANUP_INTERVAL_SEC` | `3600` | How often cleanup runs (seconds) |
| `WX_CLEANUP_MIN_AGE_SEC` | `300` | Minimum job age before files are deleted |

---

## Performance (Apple M3 Pro, CPU only)

| Audio duration | Transcription | Diarization | Total |
|---|---|---|---|
| 72 min | ~18 min | ~35 min | ~55 min |

- Model: `small`, compute type: `int8`
- Docker Desktop: 12 CPUs, 16 GB RAM
- GPU/Neural Engine not available inside Docker on macOS (Hypervisor limitation)

---

## Useful commands

```bash
# View worker logs in real-time
docker logs n8n_whisperx_worker --follow

# Check Redis queue length
docker exec n8n_stack_redis redis-cli LLEN wx:queue

# Inspect a job on Redis
docker exec n8n_stack_redis redis-cli HGETALL wx:job:JOB_ID

# List all files in MinIO bucket
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD > /dev/null &&
  mc ls --recursive local/wx-transcriptions
'

# Reset all test data (Redis + PostgreSQL + MinIO + disk)
./reset.sh
```

---

## Services & ports

| Service | Port | URL |
|---|---|---|
| n8n | 5678 | http://localhost:5678 |
| Web UI | 8081 | http://localhost:8081 |
| Whisper API | 9200 | http://localhost:9200 |
| SSE Broker | 9400 | http://localhost:9400 |
| MinIO API | 9000 | http://localhost:9000 |
| MinIO Console | 9001 | http://localhost:9001 |
| Ollama | 11434 | http://localhost:11434 |
| Qdrant | 6333 | http://localhost:6333 |

---

## Roadmap

- [x] Async transcription pipeline (faster-whisper + pyannote)
- [x] Speaker diarization with real-time progress
- [x] Dual output format (JSON + TXT)
- [x] Job queue with stale recovery
- [x] Cleanup worker for temporary files
- [x] Human-readable MinIO folder naming
- [x] SSE broker for real-time browser updates
- [x] Web UI with live job status and pipeline steps
- [x] Pause / Resume jobs
- [x] Delete job resources (4 scopes: audio / transcript / rag / purge)
- [x] DELETED status — soft delete with metadata retention
- [ ] Speaker rename (participants → SPEAKER_00 mapping)
- [ ] RAG indexing of transcripts into Qdrant
- [ ] Minute generation via Ollama
- [ ] n8n workflow for end-to-end automation