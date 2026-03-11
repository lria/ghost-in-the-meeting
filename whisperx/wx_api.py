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
import boto3
from botocore.exceptions import ClientError
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

MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY",  "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY",  "minioadmin")
MINIO_BUCKET     = os.getenv("MINIO_BUCKET",       "wx-transcriptions")
MINIO_SECURE     = os.getenv("MINIO_SECURE",       "false").lower() == "true"

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
               max_speakers, output_json_url, audio_url, error_message,
               created_at, updated_at, started_at, finished_at
        FROM wx_transcription_jobs WHERE job_id = %s
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
    if not row:
        return None
    cols = ["job_id","status","step","customer","project","language",
            "max_speakers","output_json_url","audio_url","error_message",
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
            payload["output_json_url"] = pg_row.get("output_json_url")
            payload["audio_url"]       = pg_row.get("audio_url") or r.hget(k, "audio_url") or ""
            payload["finished_at"]     = pg_row.get("finished_at")
        else:
            payload["output_json_url"] = r.hget(k, "output_json_url") or ""
            payload["audio_url"]       = r.hget(k, "audio_url") or ""
        payload["output_txt_url"]  = r.hget(k, "output_txt_url")  or ""
        payload["output_format"]   = r.hget(k, "output_format")   or "both"

    if status == "DELETED":
        # Job svuotato — solo metadati, nessun file
        payload["output_json_url"] = ""
        payload["output_txt_url"]  = ""
        payload["audio_url"]       = ""

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


@app.post("/jobs/{job_id}/pause")
def pause_job(job_id: str):
    """
    Richiede al worker di fermarsi allo step corrente.
    Setta stop_requested=1 su Redis — il worker lo legge entro qualche secondo
    e transisce il job in PAUSED.
    Funziona solo se il job è in stato WORKING.
    """
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or ""
    if status != "WORKING":
        return JSONResponse(status_code=409,
                            content={"error": "not_working",
                                     "detail": f"il job è in stato {status}, non WORKING",
                                     "job_id": job_id})

    r.hset(k, mapping={
        "stop_requested": "1",
        "updated_at":     utc_now(),
    })
    print(f"[api] stop_requested=1 → job {job_id}", flush=True)
    return {"job_id": job_id, "stop_requested": True,
            "detail": "il worker si fermerà entro qualche secondo"}


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str):
    """
    Riprende un job in stato PAUSED ri-accodandolo su Redis.
    Verifica che il file audio di input sia ancora presente su disco.
    Se il cleaner lo ha cancellato restituisce un errore chiaro con suggerimento.
    """
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or ""
    if status != "PAUSED":
        return JSONResponse(status_code=409,
                            content={"error": "not_paused",
                                     "detail": f"il job è in stato {status}, non PAUSED",
                                     "job_id": job_id})

    input_file = r.hget(k, "input_file") or ""
    if not input_file or not Path(input_file).exists():
        # Il cleaner ha già rimosso il file — informa con dettaglio utile
        return JSONResponse(status_code=410,
                            content={
                                "error":   "input_file_gone",
                                "detail":  "il file audio originale è stato rimosso dal cleaner",
                                "suggestion": "ri-invia il job con POST /jobs allegando l'audio",
                                "job_id":  job_id,
                                "input_file": input_file,
                            })

    r.hset(k, mapping={
        "status":         "PENDING",
        "step":           "RESUMED",
        "stop_requested": "0",
        "updated_at":     utc_now(),
        "error":          "",
    })
    r.rpush(QUEUE_KEY, job_id)
    print(f"[api] job {job_id} RESUMED → messo in coda", flush=True)
    return {"job_id": job_id, "status": "PENDING", "step": "RESUMED"}



def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        verify=MINIO_SECURE,
    )

def s3_delete_url(s3, url: str):
    """Cancella un oggetto MinIO dato il suo URL pubblico. Ignora errori 404."""
    if not url or not url.startswith(MINIO_ENDPOINT):
        return
    # es: http://minio:9000/wx-transcriptions/Acme/Proj/20260307_173045_abc/result.json
    path = url[len(MINIO_ENDPOINT):].lstrip("/")
    # path = "wx-transcriptions/Acme/..."
    parts = path.split("/", 1)
    if len(parts) != 2:
        return
    bucket, key = parts
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        print(f"[api] deleted s3://{bucket}/{key}", flush=True)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchKey"):
            raise


# ── DELETE /jobs/{job_id} ─────────────────────────────────────────────────────

@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, scope: str = "transcript"):
    """
    scope=audio       → cancella solo il file audio (MinIO + disco)
    scope=transcript  → audio + file trascrizione (JSON/TXT da MinIO)
    scope=rag         → transcript + Qdrant + speaker_aliases + minutes_jobs → status DELETED
    scope=purge       → rimuove completamente Redis + PostgreSQL (solo su job DELETED)
    """
    try:
        return _delete_job_impl(job_id, scope)
    except Exception as e:
        import traceback
        print(f"[api] DELETE /jobs/{job_id} unhandled: {traceback.format_exc()}", flush=True)
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(e)})


def _delete_job_impl(job_id: str, scope: str):
    if scope not in ("audio", "transcript", "rag", "purge"):
        return JSONResponse(status_code=400,
                            content={"error": "invalid_scope",
                                     "detail": "scope deve essere: audio | transcript | rag | purge"})

    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    current_status = r.hget(k, "status") or ""

    # purge solo su job DELETED
    if scope == "purge" and current_status != "DELETED":
        return JSONResponse(status_code=409,
                            content={"error": "not_deleted",
                                     "detail": "purge è disponibile solo per job in status DELETED"})

    audio_url = r.hget(k, "audio_url")       or ""
    json_url  = r.hget(k, "output_json_url") or ""
    txt_url   = r.hget(k, "output_txt_url")  or ""

    deleted = []
    errors  = []
    s3 = s3_client()

    def _s3_del(url):
        if not url:
            return
        try:
            s3_delete_url(s3, url)
            deleted.append(url)
        except Exception as e:
            errors.append(str(e))

    def _local_del():
        input_file = r.hget(k, "input_file") or ""
        if not input_file:
            return
        try:
            p = Path(input_file)
            if p.exists():
                p.unlink()
                deleted.append(input_file)
            if p.parent.exists() and not any(p.parent.iterdir()):
                p.parent.rmdir()
        except Exception as e:
            errors.append(f"local file: {e}")

    # ── audio ─────────────────────────────────────────────────────────────────
    if scope in ("audio", "transcript", "rag"):
        _s3_del(audio_url)
        _local_del()
        r.hset(k, mapping={"audio_url": "", "input_file": "", "updated_at": utc_now()})
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE wx_transcription_jobs SET audio_url=NULL WHERE job_id=%s", (job_id,))
        except Exception as e:
            errors.append(f"pg audio: {e}")

    # ── transcript (JSON + TXT) ───────────────────────────────────────────────
    if scope in ("transcript", "rag"):
        _s3_del(json_url)
        _s3_del(txt_url)
        r.hset(k, mapping={"output_json_url": "", "output_txt_url": "", "updated_at": utc_now()})
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE wx_transcription_jobs SET output_json_url=NULL WHERE job_id=%s", (job_id,))
        except Exception as e:
            errors.append(f"pg transcript: {e}")

    # ── rag: Qdrant + speaker_aliases + minutes_jobs → status DELETED ─────────
    if scope == "rag":
        # Qdrant — elimina chunks indicizzati per questo job
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            QDRANT_URL        = os.getenv("QDRANT_URL",        "http://qdrant:6333")
            QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "transcripts")
            qc = QdrantClient(url=QDRANT_URL)
            qc.delete(
                collection_name=QDRANT_COLLECTION,
                points_selector=Filter(
                    must=[FieldCondition(key="job_id", match=MatchValue(value=job_id))]
                ),
            )
            deleted.append(f"qdrant:{QDRANT_COLLECTION}:{job_id}")
        except Exception as e:
            errors.append(f"qdrant: {e}")

        # speaker_aliases e minutes_jobs (cascade da wx_transcription_jobs ma li
        # cancelliamo esplicitamente per non toccare la riga principale)
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM speaker_aliases WHERE job_id=%s", (job_id,))
                cur.execute("DELETE FROM minutes_jobs   WHERE job_id=%s", (job_id,))
        except Exception as e:
            errors.append(f"pg rag tables: {e}")

        # Imposta status DELETED — il job resta visibile nello storico
        r.hset(k, mapping={"status": "DELETED", "step": "DELETED", "updated_at": utc_now()})
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE wx_transcription_jobs SET status='DELETED' WHERE job_id=%s", (job_id,))
        except Exception as e:
            errors.append(f"pg status: {e}")

    # ── purge: rimuove completamente Redis + PostgreSQL ───────────────────────
    if scope == "purge":
        r.delete(k)
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM wx_transcription_jobs WHERE job_id=%s", (job_id,))
        except Exception as e:
            errors.append(f"pg purge: {e}")

    return {
        "job_id":  job_id,
        "scope":   scope,
        "deleted": deleted,
        "errors":  errors,
    }