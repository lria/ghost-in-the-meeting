## [2026-03-11] — Delete job (4 scope) + Pause/Resume + UI v3

### New features

#### DELETE `/jobs/{job_id}?scope=` — 4-scope delete (`wx_api.py`)
New endpoint to progressively delete job resources:

| scope | What it deletes |
|---|---|
| `audio` | Audio file from MinIO + local disk |
| `transcript` | Audio + JSON/TXT from MinIO |
| `rag` | Transcript + Qdrant chunks + `speaker_aliases` + `minutes_jobs` → sets `status=DELETED` |
| `purge` | Removes job completely from Redis + PostgreSQL (only allowed on `DELETED` jobs) |

Error handling: wrapped in try/except to always return JSON (prevents silent crash → "Load failed" in browser).

#### `DELETED` job status — `wx_api.py` + `004_add_deleted_status.sql`
Jobs deleted with `scope=rag` remain visible in the history panel with metadata only (no file links).
New migration adds `DELETED` value to `transcription_status` enum.

#### Pause/Resume jobs — `wx_api.py` + `wx_worker.py` + `wx_cleanup.py`
- `POST /jobs/{job_id}/pause` — sets `status=PAUSED`, worker skips paused jobs on dequeue
- `POST /jobs/{job_id}/resume` — sets `status=PENDING`, re-enqueues to Redis queue
- Cleanup worker never touches `PAUSED` jobs (audio file required for resume)
- Resume requires audio file on disk; returns HTTP 410 if already cleaned up

#### Transcription progress % — `wx_worker.py`
Worker now emits granular step labels during diarization (e.g. `DIARIZING_EMBEDDINGS_42pct`).
UI parses these to show a real progress bar percentage instead of indeterminate animation.

---

### UI — Web UI v3 (`webform/index.html`)

#### Delete overlay popup
Click on **Elimina** opens a centered modal overlay with scope options:
- Jobs `COMPLETED/FAILED/PAUSED`: 3 options — *Cancella audio*, *Cancella trascrizione*, *Cancella tutto (mantieni storico)*
- Jobs `DELETED`: single option — *Elimina definitivamente* (purge)

After each action, `jobsMap` is updated locally without full page reload.

#### Status badges
Added `DELETED` (grey), `PAUSED` (purple) badges to `badgeHtml()`.

#### Sospendi / Riprendi buttons
- Active job panel shows **Sospendi** (pause icon) and **Elimina** buttons
- Paused job panel shows a purple "Trascrizione sospesa" box + **Riprendi** + **Elimina** buttons
- Button label restored on resume error (no stuck disabled state)

#### History accordion — removed `...` context menu
Replaced with inline `btn-action` buttons inside the accordion body:
- `COMPLETED`: **Associa Speaker** + **Genera Minuta**
- `FAILED`: **Riprova** (retry)
- `DELETED`: grey info box + **Elimina definitivamente**

---

### Database

#### `003_add_paused_status.sql` _(new)_
Adds `PAUSED` to `transcription_status` enum + `paused_at timestamptz` column on `wx_transcription_jobs`.

#### `004_add_deleted_status.sql` _(new)_
Adds `DELETED` to `transcription_status` enum. Idempotent.

---

## [2026-03-09] — SSE Broker + Web UI v2

### New services

#### SSE Broker — `broker/sse_broker.py` + `broker/dockerfile.sse` _(new)_
Lightweight FastAPI microservice that exposes real-time job events to the browser via Server-Sent Events.
Reads job state from Redis (HASH `wx:job:{id}`) by polling every 2s and pushes only deltas.
**Zero modifications** to `wx_worker.py`, `wx_api.py`, `wx_cleanup.py`.

Endpoints:
- `GET /health` — broker status and Redis connectivity
- `GET /jobs` — full snapshot of all jobs (used on page load)
- `GET /events?all=1` — SSE stream for all jobs
- `GET /events?jobs=id1,id2` — SSE stream filtered by job IDs

External port: `9400`.

#### Web UI v2 — `webform/index.html` _(rewritten)_
Two-column interface with live updates via SSE.

**Left column — Nuova Trascrizione:**
- Drag-and-drop audio upload
- Fields: customer, project, participants, language, max speakers, output format, diarization toggle
- Submits via `POST n8n :5678/webhook/minute/ingest`

**Right column — Stato Trascrizioni:**
- **Trascrizione Corrente** section: job title in format `Customer — Project — del DD/MM/YYYY HH:MM`, status badge, progress bar, start/updated timestamps, animated pipeline steps (done / active / pending), "IN CODA" badge when other jobs are queued
- **Storico Trascrizioni** section: accordion per completed/failed job — date, status, JSON/TXT download links, error box with retry button
- Stream status badge (Live / Connecting / Idle / Disconnected) with auto-reconnect
- Health indicators for whisperx API and SSE broker in the header

### Docker
- Added `sse_broker` service on port `9400`
- Build context: `.`, dockerfile: `broker/dockerfile.sse`
- Volume: `./broker/sse_broker.py:/app/sse_broker.py:ro`

---

## [2026-03-07] — Audio Transcription Pipeline: Fixes & New Services

### Bug fixes

#### Diarization hook — `wx_worker.py`
The pyannote `Hooks` class requires each hook to be called as a context manager (`with` statement)
and expects a specific callback signature:

```python
def callback(step_name, step_artifact, file=None, total=None, completed=None)
```

Three issues were causing silent diarization failures:

- **Wrong import**: `Hook` does not exist in pyannote 3.3.2 — correct class is `Hooks` (plural)
- **Wrong signature**: callback was declared as `(step_name, **kwargs)` or `(step_name, completed, total, **kwargs)`,
  causing `TypeError: got multiple values for argument 'completed'`
- **Missing context manager**: `Hooks` must be used with `with` — calling it directly as
  `hook=Hooks(_progress)` skips `__enter__`/`__exit__` lifecycle

Fix:
```python
def _progress(step_name, step_artifact, file=None, total=None, completed=None):
    if total and completed is not None and total > 0:
        pct = int(completed / total * 100)
        print(f"[worker] diarizzazione {step_name}: {pct}%", flush=True)
        update_job(job_id, step=f"DIARIZING_{step_name.upper()}_{pct}pct")

with Hooks(_progress) as hook:
    diarization = diar_pipeline(str(wav_path), max_speakers=max_spk, hook=hook)
```

---

### New features

#### MinIO folder naming
Output files are now stored under a human-readable timestamp folder instead of raw `job_id`:

```
# Before
Acme/Board/de10e2332e0849139b1c9ef7bbc34a88/result.json

# After
Acme/Board/20260307_173045_de10e233/result.json
```

Format: `YYYYMMDD_HHMMSS_{job_id[:8]}`

#### Dual output format — `output_format` parameter _(new)_
`POST /jobs` now accepts an `output_format` form field:

| Value | Output |
|---|---|
| `json` | `result.json` only |
| `txt` | `result.txt` only |
| `both` | both files (default) |

#### TXT format with gap-based speaker blocks
The human-readable TXT output now starts a new speaker block when:
- The speaker label changes (diarization active), **or**
- There is a silence gap > 2 seconds between segments (diarization disabled or unknown speaker)

This ensures readable output even when diarization is skipped.

#### All API parameters moved to `Form`
All `POST /jobs` parameters are now `Form` fields (multipart body).
Previously `customer`, `project`, `participants` were `Form` while others were `Query` params in the URL.

| Parameter | Type | Default |
|---|---|---|
| `file` | File | required |
| `customer` | str | required |
| `project` | str | required |
| `language` | str | `it` |
| `max_speakers` | int | `8` |
| `diarize` | bool | `true` |
| `output_format` | str | `both` |
| `participants` | str | `""` |

#### `GET /jobs/{job_id}` response fields renamed
- `output_minio_url` → `output_json_url`
- Added `output_txt_url`
- Added `output_format`

#### Cleanup worker — `wx_cleanup.py` _(new)_
A dedicated `whisperx_cleanup` service handles periodic deletion of temporary files
on disk (input audio + local output) for jobs already uploaded to MinIO.

Only terminal jobs (`COMPLETED` or `FAILED`) are eligible for cleanup.
Active jobs (`WORKING`, `PENDING`) are always skipped regardless of age.

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `WX_CLEANUP_INTERVAL_SEC` | `3600` | Cleanup cycle interval in seconds |
| `WX_CLEANUP_MIN_AGE_SEC` | `300` | Minimum job age before cleanup (seconds) |

#### MinIO bucket — public download
Bucket `wx-transcriptions` is now set to anonymous `download` policy,
allowing direct URL access without credentials.

#### `reset.sh` _(new)_
Script to wipe all test data: Redis jobs, PostgreSQL rows, MinIO bucket contents,
and local audio files — without touching models, containers, or configuration.

---

### Database

- Column `output_minio_url` renamed to `output_json_url` in `wx_transcription_jobs`
- `002_create_wx_transcription_jobs.sql` removed — unified into `001_create_transcription_runs.sql`

---

### Docker

- `TRANSFORMERS_CACHE` removed from `whisperx_worker` environment (deprecated, `HF_HOME` is sufficient)
- `ORT_LOGGING_LEVEL=3` added to suppress onnxruntime ARM64 CPU vendor warning
- New service `whisperx_cleanup` added — shares the same image as `whisperx_worker`,
  mounts only `wx_cleanup.py` and `./data/uploads`