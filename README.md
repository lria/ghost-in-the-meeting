# 👻 Ghost in the Meeting

An self-hosted, async pipeline for transcribing and diarizing meeting audio, built on Apple Silicon (M3 Pro) with Docker.

Transcribes audio in Italian and English, identifies speakers, and produces structured output ready for minute generation via RAG + LLM.

---

## Stack

| Service | Role |
|---|---|
| **faster-whisper** | Speech-to-text transcription |
| **pyannote.audio** | Speaker diarization |
| **n8n** | Workflow orchestration |
| **PostgreSQL** | Job tracking and metadata |
| **Redis** | Job queue (FIFO) |
| **MinIO** | Output storage (JSON + TXT) |
| **Qdrant** | Vector store (RAG, upcoming) |
| **Ollama** | Local LLM for minute generation (upcoming) |

---

## Architecture

```
User / n8n
    │
    ▼
whisperx_api          (FastAPI — port 9200)
    │  POST /jobs      → saves audio to disk, enqueues job on Redis
    │  GET  /jobs/:id  → polls status + returns output URLs
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

MINIO_ROOT_USER=admin
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

**4. First run — build takes ~10 minutes** (downloads PyTorch + pyannote models)

---

## API

Base URL: `http://localhost:9200`

### `POST /jobs` — Submit a transcription job

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

**Response while processing:**
```json
{
  "job_id": "d87b69740f634fd28ad1d5dda935e963",
  "status": "WORKING",
  "step": "DIARIZING_EMBEDDINGS_42pct"
}
```

**Response when completed:**
```json
{
  "job_id": "d87b69740f634fd28ad1d5dda935e963",
  "status": "COMPLETED",
  "output_json_url": "http://localhost:9000/wx-transcriptions/Acme/Q1Review/.../result.json",
  "output_txt_url":  "http://localhost:9000/wx-transcriptions/Acme/Q1Review/.../result.txt",
  "finished_at": "2026-03-07T17:30:00Z"
}
```

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

## Output formats

### JSON (`result.json`)
Full structured output with word-level timestamps and speaker labels per segment.

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
Human-readable transcript grouped by speaker turn.

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

## Performance (Apple M3 Pro, CPU only)

| Audio duration | Transcription | Diarization | Total |
|---|---|---|---|
| 72 min | ~18 min | ~35 min | ~55 min |

- Model: `small`, compute type: `int8`
- Docker Desktop: 12 CPUs, 16 GB RAM
- GPU access not available inside Docker on macOS (Hypervisor limitation)

---

## Useful commands

```bash
# View worker logs in real-time
docker logs n8n_whisperx_worker --follow

# Check Redis queue
docker exec n8n_stack_redis redis-cli LLEN wx:queue

# Inspect a job on Redis
docker exec n8n_stack_redis redis-cli HGETALL wx:job:JOB_ID

# Reset all test data (Redis + PostgreSQL + MinIO + disk)
./reset.sh

# Access MinIO console
open http://localhost:9001

# Access n8n
open http://localhost:5678
```

---

## Services & ports

| Service | Port | URL |
|---|---|---|
| n8n | 5678 | http://localhost:5678 |
| Whisper API | 9200 | http://localhost:9200 |
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
- [ ] Speaker rename (participants → SPEAKER_00 mapping)
- [ ] RAG indexing of transcripts into Qdrant
- [ ] Minute generation via Ollama
- [ ] Web form for audio upload
- [ ] n8n workflow for end-to-end automation
