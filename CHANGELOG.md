## [2026-03-18] — Bug fix globale: Redis, MinIO URL parsing, Speaker mapping, History UX

### Bug fixes

#### `wx_worker.py` — Redis connection drop durante diarizzazione lunga
La connessione TCP con Redis veniva resettata da Docker dopo 35+ minuti di diarizzazione.
Il worker crashava su `blpop()` al termine del job, rendendo il container in loop di restart.

**Causa:** nessun keepalive configurato sul client Redis; il layer di rete Docker chiudeva le connessioni idle.

**Fix:**
- `redis.Redis.from_url(...)` ora include `socket_keepalive=True` e `health_check_interval=30`
- Il loop `main()` cattura `redis.ConnectionError` e `redis.TimeoutError` con retry dopo 5s invece di crashare
- I job già in `WORKING` al momento del crash vengono recuperati dal meccanismo `recover_stale_jobs()` al riavvio (finestra: 45 min)

#### `wx_api.py` — Endpoint DELETE, pause e resume mancanti
Il frontend chiamava `DELETE /jobs/{id}?scope=`, `POST /jobs/{id}/pause` e `POST /jobs/{id}/resume` ma il backend rispondeva **405 Method Not Allowed** perché i route non erano implementati.

**Aggiunti:**

- `POST /jobs/{job_id}/pause` — setta `stop_requested=1` su Redis; il worker lo rileva nel loop di trascrizione/diarizzazione e mette il job in `PAUSED` preservando il file audio su disco. Se il job era ancora `PENDING` (non preso dal worker) lo mette direttamente in `PAUSED`.
- `POST /jobs/{job_id}/resume` — verifica che il file audio sia ancora su disco (ritorna 410 se rimosso dal cleanup), poi setta `status=PENDING` e re-accoda.
- `DELETE /jobs/{job_id}?scope=` — elimina risorse in base allo scope:

  | scope | Cosa elimina |
  |---|---|
  | `audio` | File audio da MinIO + cartella `data/in/{job_id}` su disco |
  | `transcript` | scope audio + result.json + result.txt da MinIO + `data/out/{job_id}` |
  | `rag` | scope transcript + punti Qdrant + `speaker_aliases` + `minutes_jobs` + `rag_index_jobs` + file `.md` dal bucket `minutes` → `status=DELETED` (soft delete) |
  | `purge` | Solo su job già `DELETED`: rimuove `wx:job:{id}` da Redis + `DELETE FROM wx_transcription_jobs` in PG (cascade su tutte le tabelle figlie) |

#### `transcriber_api.py` — URL MinIO parsing errato in `get_job_speakers` e `get_transcript_text`
Gli URL dei file sono salvati usando `MINIO_PUBLIC_URL` (`http://localhost:9000/...`) ma il parsing cercava di rimuovere `MINIO_ENDPOINT` (`http://minio:9000/...`). Il `replace()` non trovava nulla e `split("/", 1)` sul URL intero produceva `"http:"` come bucket name, causando l'errore boto3 `Invalid bucket name "http:"`.

**Fix:** aggiunto helper `_parse_minio_url(url)` basato su `urllib.parse.urlparse` che estrae `(bucket, key)` indipendentemente dall'host dell'URL. Sostituisce il `replace(MINIO_ENDPOINT + "/", "").split("/", 1)` in `get_job_speakers_from_redis` e `get_transcript_text`.

#### `minutes_worker.py` — Doppio bug: URL MinIO e speaker mapping vuoto
1. **URL parsing** — stessa radice del bug `transcriber_api.py`: `fetch_transcript` usava `replace(MINIO_ENDPOINT, "")` → bucket = `"http:"` → il fetch falliva e cadeva sul fallback scan (più lento e non sempre affidabile).
2. **Speaker mapping vuoto** — `participants` nel job Redis era sempre `[]` perché `minutes.html` chiamava `fetchParticipants` su `WHISPERX_API` (porta 9200, `wx_api`) invece di `TRANSCRIBER_API` (porta 9500, `transcriber_api`). La chiamata ritornava 404 e veniva silenziata.

**Fix:**
- Aggiunto `_parse_minio_url()` in `minutes_worker.py` (stessa logica di `transcriber_api.py`)
- Aggiunto `get_speaker_aliases_as_participants(job_id)` che legge direttamente da `speaker_aliases` in PostgreSQL — sorgente di verità primaria, prioritaria rispetto ai `participants` passati da Redis
- In `run_job()`: se PG restituisce alias → usati; se no → fallback su Redis; se no → log warning

#### `webform/minutes.html` — `fetchParticipants` su porta sbagliata
`fetchParticipants` chiamava `${WHISPERX_API}/jobs/${jobId}/speakers` (porta 9200) invece di `${TRANSCRIBER_API}/jobs/${jobId}/speakers` (porta 9500). Il risultato era sempre `[]` silenzioso.

**Fix:** aggiunta costante `TRANSCRIBER_API = "http://localhost:9500"` e corretta la chiamata in `fetchParticipants`.

> **Nota:** il fix principale è nel worker (legge sempre da PG), ma la correzione del frontend garantisce che `participants` sia corretto anche nel job Redis per eventuali usi futuri.

#### `webform/history.html` — Conferma prima dell'eliminazione
Il click su un'opzione di eliminazione nel modale invocava immediatamente `execDelete` senza chiedere conferma.

**Fix:** aggiunto step intermedio "Sei sicuro?" inline nel modale (nessun `alert()` nativo):
- Click opzione → schermo di conferma con icona ⚠, label e descrizione dell'azione, avviso "non può essere annullata"
- **← Indietro** → ripristina le opzioni originali
- **Conferma eliminazione** → esegue il DELETE

---

## [2026-03-12] — Minutes AI: Template System + MD Preview + Per-Job Minute List

### New features

#### Template system — `minutes/templates/`
Template files are `.txt` with a structured header:

```
Title: Nome template
Tags: tag1, tag2, tag3
Description: Descrizione per il tooltip
---
<prompt con variabili {{ customer }}, {{ transcript }}, ecc.>
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