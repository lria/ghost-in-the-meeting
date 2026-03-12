## [2026-03-12] — Minutes AI: Template System + MD Preview + Per-Job Minute List

### New features

#### Template system — `minutes/templates/`
Template files are `.txt` with a structured header:

```
Title: Nome template
Tags: tag1, tag2, tag3
Description: Descrizione per il tooltip
---
<prompt Jinja2>
```

Three templates ship out of the box:

| File | Name | Use case |
|---|---|---|
| `standard.txt` | Verbale Standard | Riunioni interne, sprint review, project update |
| `minuta_dettagliata.txt` | Minuta Dettagliata | Sessioni strategiche, kickoff, workshop |
| `1on1.txt` | 1:1 | Colloqui 1:1 — niente prosa, solo punti e next step |

Templates are reloaded on `minutes_api` / `minutes_worker` restart. Falls back to hardcoded templates if directory is empty or missing.

#### Per-job minute list — `webform/minutes.html` _(rewritten)_
`minutes.html` now shows all minutes for a **single transcription job** passed via `?job_id=` from `index.html`. One row per template (not per minutes_id).

Each row shows template name + triplet chips (**template · model · context level**) + status badge.

For active jobs: animated progress bar + step label + vertical pipeline steps.
For completed jobs: preview button + download button.
For failed jobs: inline error + retry.

Progress bar is step-weighted:

| Step | Bar |
|---|---|
| ENQUEUED | 2% |
| FETCHING | 10% |
| RAG_RETRIEVAL | indeterminate |
| BUILDING_PROMPT | 40% |
| GENERATING | indeterminate |
| UPLOADING | 92% |
| COMPLETED | 100% |

Clicking a completed row syncs the left panel (template, model, context) with the settings of that minute and opens the MD preview.

#### Markdown preview
Completed minutes render as formatted HTML inline — no external libraries.
Supports: H1–H4, bold, italic, lists, GFM tables, blockquotes, HR, code, ordered lists.
Download (.md) and Copy to clipboard in the preview card header.

#### `index.html` — button label
- **Genera minuta** — no minutes exist for this job
- **Gestisci minuta** — at least one minute exists

#### New templates
- `minuta_dettagliata.txt` — forces the model to develop each topic with specific data, context, participant positions, risks, open questions
- `1on1.txt` — no prose, bullet lists only: work in progress, blockers, next steps table with owner and deadline

### Bug fixes

- **JS syntax error** `alert('C'è…')` broke the entire script — fixed to double quotes
- **`loadFixedJob`** was querying SSE broker which excludes COMPLETED jobs — now reads from `whisperx_api GET /jobs/{job_id}` directly
- **`loadTemplates`** silently produced empty grid on API failure — now falls back to hardcoded templates
- **`loadModels`** showed only error message on Ollama unreachable — now shows suggested models with Pull buttons
- **`GET /minutes/jobs`** missing `step`, `context_level`, `model` in response — added
- **`POST /minutes/jobs`** now returns HTTP 409 if a PENDING/WORKING job exists for same `(job_id, template)` pair

---

## [2026-03-09] — SSE Broker + Web UI v2

### New services

#### SSE Broker — `broker/sse_broker.py` + `broker/dockerfile.sse` _(new)_
Lightweight FastAPI microservice exposing real-time job events via Server-Sent Events.
Polls Redis (`wx:job:{id}`) every 2s, pushes only deltas. Zero modifications to worker/api/cleanup.

Endpoints: `GET /health`, `GET /jobs`, `GET /events?all=1`, `GET /events?jobs=id1,id2`

External port: `9400`.

#### Web UI v2 — `webform/index.html` _(rewritten)_
Two-column layout with live SSE updates. Left: upload form. Right: live current job + history accordion with download links and retry.

### Docker
- Added `sse_broker` service on port `9400`

---

## [2026-03-07] — Audio Transcription Pipeline: Fixes & New Services

### Bug fixes — `wx_worker.py`
- Wrong import `Hook` → `Hooks` (plural) in pyannote 3.3.2
- Wrong callback signature causing `TypeError: got multiple values for argument 'completed'`
- Missing `with` context manager on `Hooks`

### New features
- MinIO folder naming: `YYYYMMDD_HHMMSS_{job_id[:8]}`
- `output_format` parameter: `json` | `txt` | `both`
- TXT speaker blocks on silence gap > 2s
- Cleanup worker `wx_cleanup.py` — periodic disk cleanup for terminal jobs
- MinIO bucket `wx-transcriptions` set to anonymous download policy

### Database
- `output_minio_url` → `output_json_url`
- Schema unified into `001_create_transcription_runs.sql`

### Docker
- New service `whisperx_cleanup`
- `ORT_LOGGING_LEVEL=3` to suppress onnxruntime ARM64 warning