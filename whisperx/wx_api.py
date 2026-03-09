"""
wx_api.py — WhisperX Jobs API
Endpoints:
  POST /jobs            upload audio → crea job → mette in coda Redis
  GET  /jobs/{job_id}   polling stato + risultato (link MinIO quando COMPLETED)
  POST /jobs/{job_id}/retry  ri-accoda un job FAILED/STUCK
  GET  /health
"""

import os
import uuid
import shutil
import psycopg2
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import redis

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL  = os.getenv("REDIS_URL",       "redis://redis:6379/0")
QUEUE_KEY  = os.getenv("WX_QUEUE_KEY",    "wx:queue")

DATA_DIR   = Path(os.getenv("WX_DATA_DIR", "/data"))
IN_DIR     = DATA_DIR / "in"
IN_DIR.mkdir(parents=True, exist_ok=True)

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

# ── Clients ───────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
app = FastAPI(title="WhisperX Jobs API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # in produzione restringi a http://localhost:8081
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def job_key(job_id: str) -> str:
    return f"wx:job:{job_id}"

def pg_conn():
    return psycopg2.connect(PG_DSN)

def pg_insert_job(job_id: str, customer: str, project: str,
                  language: str, max_speakers: int, input_filename: str):
    """Inserisce la riga iniziale in PostgreSQL."""
    sql = """
        INSERT INTO wx_transcription_jobs
          (job_id, status, step, customer, project, language,
           max_speakers, input_filename, created_at, updated_at)
        VALUES (%s,'PENDING','ENQUEUED',%s,%s,%s,%s,%s,now(),now())
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (job_id, customer, project,
                          language, max_speakers, input_filename))

def pg_get_job(job_id: str) -> dict | None:
    sql = """
        SELECT job_id, status, step, customer, project, language,
               max_speakers, output_json_url, error_message,
               created_at, updated_at, started_at, finished_at
        FROM wx_transcription_jobs WHERE job_id = %s
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
    if not row:
        return None
    cols = ["job_id","status","step","customer","project","language",
            "max_speakers","output_json_url","error_message",
            "created_at","updated_at","started_at","finished_at"]
    return {c: (v.isoformat() if hasattr(v, "isoformat") else v)
            for c, v in zip(cols, row)}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    queue_len = r.llen(QUEUE_KEY)
    return {"ok": True, "queue": QUEUE_KEY, "queued_jobs": queue_len}


@app.get("/queue-stats")
def queue_stats():
    """
    Snapshot completo dello stato della coda.
    Utile per debug e monitoring da n8n o browser.
    """
    queued_ids = r.lrange(QUEUE_KEY, 0, -1)

    counts = {"PENDING": 0, "WORKING": 0, "COMPLETED": 0, "FAILED": 0}
    stale  = []  # job WORKING da > 30 min (probabilmente bloccati)

    for key in r.scan_iter(match="wx:job:*", count=200):
        status     = r.hget(key, "status") or "OTHER"
        job_id     = r.hget(key, "job_id") or key.split(":")[-1]
        updated_at = r.hget(key, "updated_at") or ""

        counts[status] = counts.get(status, 0) + 1

        if status == "WORKING" and updated_at:
            try:
                ts  = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > 1800:  # 30 min
                    stale.append({
                        "job_id":     job_id,
                        "age_min":    round(age / 60, 1),
                        "updated_at": updated_at,
                    })
            except Exception:
                pass

    return {
        "queue_length":  r.llen(QUEUE_KEY),
        "queued_ids":    queued_ids,
        "status_counts": counts,
        "stale_working": stale,
    }


@app.post("/jobs", status_code=202)
async def submit_job(
    file:          UploadFile = File(...),
    customer:      str        = Form(...,         description="Nome cliente, es. Alpitour"),
    project:       str        = Form(...,         description="Nome progetto, es. AI Platform"),
    language:      str        = Form("it",        description="Codice lingua: it, en, auto"),
    max_speakers:  int        = Form(8,           description="Numero massimo speaker (1-20)"),
    diarize:       bool       = Form(True,        description="Abilita diarizzazione speaker"),
    output_format: str        = Form("both",      description="Formato output: json | txt | both"),
    participants:  str        = Form("",          description="Lista partecipanti (per speaker rename)"),
):
    job_id = uuid.uuid4().hex

    # Salva audio su disco locale (il worker lo leggerà da qui)
    job_in_dir = IN_DIR / job_id
    job_in_dir.mkdir(parents=True, exist_ok=True)
    input_filename = file.filename or "audio.bin"
    raw_path = job_in_dir / input_filename
    with raw_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Metadati su Redis (il worker li legge da qui)
    # valida output_format
    if output_format not in ("json", "txt", "both"):
        output_format = "both"

    meta = {
        "job_id":        job_id,
        "status":        "PENDING",
        "step":          "ENQUEUED",
        "customer":      customer,
        "project":       project,
        "language":      language,
        "diarize":       str(diarize).lower(),
        "max_speakers":  str(max_speakers),
        "output_format": output_format,
        "participants":  participants,
        "input_file":    str(raw_path),
        "created_at":    utc_now(),
        "updated_at":    utc_now(),
    }
    r.hset(job_key(job_id), mapping=meta)
    r.rpush(QUEUE_KEY, job_id)  # FIFO queue

    # Riga iniziale su PostgreSQL
    try:
        pg_insert_job(job_id, customer, project, language,
                      max_speakers, input_filename)
    except Exception as e:
        # Non bloccare il job se PG non è disponibile — Redis basta per il worker
        print(f"[api] PG insert warning: {e}", flush=True)

    return {
        "job_id":        job_id,
        "status":        "PENDING",
        "customer":      customer,
        "project":       project,
        "output_format": output_format,
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or "UNKNOWN"

    payload = {
        "job_id":     job_id,
        "status":     status,
        "step":       r.hget(k, "step")       or "",
        "customer":   r.hget(k, "customer")   or "",
        "project":    r.hget(k, "project")    or "",
        "created_at": r.hget(k, "created_at") or "",
        "updated_at": r.hget(k, "updated_at") or "",
    }

    if status == "FAILED":
        payload["error"]  = r.hget(k, "error")  or "unknown_error"
        payload["detail"] = r.hget(k, "detail") or ""

    if status == "COMPLETED":
        # Preferisce i dati freschi da PostgreSQL (contiene URL MinIO)
        pg_row = pg_get_job(job_id)
        if pg_row:
            payload["output_json_url"]     = pg_row.get("output_json_url")
            payload["finished_at"]          = pg_row.get("finished_at")
        else:
            payload["output_json_url"]     = r.hget(k, "output_json_url") or ""
        payload["output_txt_url"]       = r.hget(k, "output_txt_url")  or ""
        payload["output_format"]        = r.hget(k, "output_format")   or "both"

    return payload


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    input_file = r.hget(k, "input_file")
    if not input_file or not Path(input_file).exists():
        return JSONResponse(status_code=400,
                            content={"error": "input_missing",
                                     "detail": "file audio non trovato su disco"})

    r.hset(k, mapping={
        "status":     "PENDING",
        "step":       "REQUEUED_MANUAL",
        "updated_at": utc_now(),
        "error":      "",
        "detail":     "",
    })
    r.rpush(QUEUE_KEY, job_id)
    return {"job_id": job_id, "status": "PENDING", "step": "REQUEUED_MANUAL"}