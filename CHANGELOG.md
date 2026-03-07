## Audio Transcription Pipeline — Refactor & Improvements

### Architecture

- **Removed WhisperX dependency** entirely. The project is no longer maintained and
  caused recurring incompatibilities with `faster-whisper >= 1.0.3` and `pyannote >= 3.2`.
  The transcription + diarization pipeline is now built directly on:
  - `faster-whisper 1.0.0` — transcription engine (actively maintained by SYSTRAN)
  - `pyannote.audio 3.3.2` — speaker diarization (actively maintained)
  - `ctranslate2 4.3.1` — inference backend, pinned for ARM64 stability

- **Docker platform changed** from `linux/amd64` (Rosetta emulation) to `linux/arm64`
  (native Apple Silicon). This resolves the `cannot enable executable stack` crash
  in ctranslate2 4.x under Rosetta and improves overall performance.

---

### API — `wx_api.py`

#### `POST /jobs` — Submit transcription job
All parameters are now `Form` fields (multipart body). Previously a mix of `Form` and `Query`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file` | File | required | Audio file (any format supported by ffmpeg) |
| `customer` | str | required | Customer name, used as MinIO namespace |
| `project` | str | required | Project name, used as MinIO namespace |
| `language` | str | `it` | Language code: `it`, `en`, `auto` |
| `max_speakers` | int | `8` | Maximum number of speakers (1–20) |
| `diarize` | bool | `true` | Enable speaker diarization via pyannote |
| `output_format` | str | `both` | Output format: `json` \| `txt` \| `both` |
| `participants` | str | `""` | Optional participant list (for future speaker rename) |

#### `GET /jobs/{job_id}` — Poll job status
Response now includes:
- `output_json_url` — MinIO URL of `result.json` (when `output_format` is `json` or `both`)
- `output_txt_url` — MinIO URL of `result.txt` (when `output_format` is `txt` or `both`)
- `output_format` — format requested at submission
- `step` — current processing step, updated in real-time during diarization

#### `GET /queue-stats` — Queue snapshot _(new)_
Returns a full snapshot of the Redis queue state:
- `queue_length` — number of jobs waiting
- `queued_ids` — list of pending job IDs
- `status_counts` — breakdown by status (PENDING, WORKING, COMPLETED, FAILED)
- `stale_working` — jobs in WORKING state for more than 30 minutes (likely stuck)

---

### Worker — `wx_worker.py`

#### Transcription — `transcribe()`
Replaced `whisperx.load_model()` + `whisperx.transcribe()` with direct `faster_whisper.WhisperModel`:
- `word_timestamps=True` — word-level timestamps for accurate diarization merge
- `vad_filter=True` — silence filtering to reduce hallucinations
- `vad_parameters={"min_silence_duration_ms": 500}`

#### Diarization progress hook _(new)_
Real-time progress reporting during pyannote diarization via `pyannote.audio.pipelines.utils.hook.Hook`.
Each internal step (`segmentation`, `embeddings`, `discrete_diarization`) now:
- Prints progress percentage to Docker logs in real-time with `flush=True`
- Updates the Redis `step` field so `GET /jobs/{job_id}` reflects live progress

#### Output generation — dual format _(new)_
The worker now generates output based on the `output_format` parameter:

- **`json`** — structured `result.json` with full segment list, speaker labels, word timestamps
- **`txt`** — human-readable `result.txt` with speaker turns, timestamps, and full transcript
- **`both`** (default) — generates and uploads both files to MinIO

TXT format example:
```
RIUNIONE:  Acme / Board
DATA:      2026-03-07
LINGUA:    it
SPEAKER:   SPEAKER_00, SPEAKER_01

────────────────────────────────────────────────────────────
[00:00:05] SPEAKER_00
Buongiorno a tutti...

[00:01:12] SPEAKER_01
Grazie, come dicevo...

────────────────────────────────────────────────────────────

TESTO COMPLETO:
Buongiorno a tutti... Grazie, come dicevo...
```

---

### Database — `001_create_transcription_runs.sql`

- **Unified** `001_create_transcription_runs.sql` and `002_create_wx_transcription_jobs.sql`
  into a single idempotent file. The `002` file is now obsolete and should be removed.
- **Column renamed**: `output_minio_url` → `output_json_url` in `wx_transcription_jobs`
- **Enum updated**: `transcription_status` now includes all required values:
  `CREATED`, `PENDING`, `WORKING`, `COMPLETED`, `FAILED`, `ERROR`
- The `DO $$ ... IF NOT EXISTS` block now adds missing enum values to existing databases
  without requiring a full reset.

---

### Docker

- `dockerfile.whisperx` — removed `libavformat-dev`, `libavcodec-dev` and other
  build-time headers no longer needed since `av==12.0.0` ships ARM64 prebuilt wheels
- `docker-compose.yml` — environment variables split cleanly between services:
  - `whisperx_api` — only networking/storage vars (Redis, MinIO, PostgreSQL)
  - `whisperx_worker` — full set including HuggingFace, Torch, thread tuning
