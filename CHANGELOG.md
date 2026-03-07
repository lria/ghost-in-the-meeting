## Audio Transcription Pipeline — Fixes & New Services

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