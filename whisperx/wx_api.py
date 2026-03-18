"""
wx_api.py — WhisperX Jobs API

Endpoints:
  POST   /jobs                    upload audio → crea job → mette in coda Redis
  GET    /jobs/{job_id}           polling stato + risultato (link MinIO quando COMPLETED)
  POST   /jobs/{job_id}/pause     setta stop_requested=1 → worker si ferma e mette PAUSED
  POST   /jobs/{job_id}/resume    re-accoda un job PAUSED (410 se audio non più su disco)
  POST   /jobs/{job_id}/retry     ri-accoda un job FAILED/STUCK
  DELETE /jobs/{job_id}?scope=    elimina risorse del job (audio | transcript | rag | purge)
  GET    /queue-stats             snapshot coda
  GET    /health

Scopes DELETE:
  audio      → audio originale da MinIO + disco locale
  transcript → audio scope + result.json + result.txt da MinIO + disco locale
  rag        → transcript scope + Qdrant points + speaker_aliases + minutes_jobs
               + rag_index_jobs + file .md da bucket minutes
               → status=DELETED in Redis e PG (soft delete, metadata intatti)
  purge      → solo su job già DELETED: rimuove wx:job:{id} da Redis
               + DELETE FROM wx_transcription_jobs (cascade elimina tutte le tabelle figlie)
"""

import os
import uuid
import shutil
import psycopg2
import requests
import boto3
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from botocore.exceptions import ClientError
import redis

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL  = os.getenv("REDIS_URL",       "redis://redis:6379/0")
QUEUE_KEY  = os.getenv("WX_QUEUE_KEY",    "wx:queue")

DATA_DIR   = Path(os.getenv("WX_DATA_DIR", "/data"))
IN_DIR     = DATA_DIR / "in"
OUT_DIR    = DATA_DIR / "out"
IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL",  "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY",  "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY",  "minioadmin")
MINIO_BUCKET     = os.getenv("MINIO_BUCKET",      "wx-transcriptions")
MINUTES_BUCKET   = os.getenv("MINUTES_BUCKET",    "minutes")
MINIO_SECURE     = os.getenv("MINIO_SECURE",      "false").lower() == "true"

QDRANT_URL        = os.getenv("QDRANT_URL",        "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "transcriptions")

# ── Clients ───────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="WhisperX Jobs API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        verify=MINIO_SECURE,
    )

def minio_key_from_url(url: str) -> str | None:
    """
    Estrae la chiave MinIO dall'URL pubblico.
    http://localhost:9000/wx-transcriptions/Acme/Q1/folder/result.json
    → Acme/Q1/folder/result.json
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # path = /bucket/key...
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2:
            return parts[1]
    except Exception:
        pass
    return None

def minio_delete_key(s3, bucket: str, key: str):
    """Elimina una chiave da MinIO, silente se non esiste."""
    if not key:
        return
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "404"):
            print(f"[api] MinIO delete warning {bucket}/{key}: {e}", flush=True)

def minio_delete_folder(s3, bucket: str, prefix: str):
    """
    Elimina tutti gli oggetti sotto un prefix (cartella) su MinIO.
    Usato per pulire l'intera cartella del job: customer/project/ts_jobid/
    """
    if not prefix:
        return
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if objects:
                s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                )
    except Exception as e:
        print(f"[api] MinIO folder delete warning {bucket}/{prefix}: {e}", flush=True)

def qdrant_delete_by_job(job_id: str):
    """
    Elimina da Qdrant tutti i punti con payload.job_id == job_id.
    Usa l'API REST di Qdrant: POST /collections/{name}/points/delete
    """
    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/delete",
            json={"filter": {"must": [{"key": "job_id", "match": {"value": job_id}}]}},
            timeout=10,
        )
        if resp.status_code not in (200, 404):
            print(f"[api] Qdrant delete warning job {job_id}: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[api] Qdrant delete warning job {job_id}: {e}", flush=True)

def pg_insert_job(job_id: str, customer: str, project: str,
                  language: str, max_speakers: int, input_filename: str):
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
    try:
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
    except Exception as e:
        print(f"[api] pg_get_job warning: {e}", flush=True)
        return None

def pg_update_job(job_id: str, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [job_id]
    sql = f"UPDATE wx_transcription_jobs SET {set_clause}, updated_at=now() WHERE job_id = %s"
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, values)
    except Exception as e:
        print(f"[api] pg_update_job warning: {e}", flush=True)


# ── Routes — health & stats ───────────────────────────────────────────────────

@app.get("/health")
def health():
    queue_len = r.llen(QUEUE_KEY)
    return {"ok": True, "queue": QUEUE_KEY, "queued_jobs": queue_len}


@app.get("/queue-stats")
def queue_stats():
    queued_ids = r.lrange(QUEUE_KEY, 0, -1)
    counts = {"PENDING": 0, "WORKING": 0, "COMPLETED": 0, "FAILED": 0}
    stale  = []

    for key in r.scan_iter(match="wx:job:*", count=200):
        status     = r.hget(key, "status") or "OTHER"
        job_id     = r.hget(key, "job_id") or key.split(":")[-1]
        updated_at = r.hget(key, "updated_at") or ""
        counts[status] = counts.get(status, 0) + 1

        if status == "WORKING" and updated_at:
            try:
                ts  = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > 1800:
                    stale.append({"job_id": job_id, "age_min": round(age/60,1), "updated_at": updated_at})
            except Exception:
                pass

    return {
        "queue_length":  r.llen(QUEUE_KEY),
        "queued_ids":    queued_ids,
        "status_counts": counts,
        "stale_working": stale,
    }


# ── Routes — submit ───────────────────────────────────────────────────────────

@app.post("/jobs", status_code=202)
async def submit_job(
    file:          UploadFile = File(...),
    customer:      str        = Form(...),
    project:       str        = Form(...),
    language:      str        = Form("it"),
    max_speakers:  int        = Form(8),
    diarize:       bool       = Form(True),
    output_format: str        = Form("both"),
    participants:  str        = Form(""),
):
    job_id = uuid.uuid4().hex

    job_in_dir = IN_DIR / job_id
    job_in_dir.mkdir(parents=True, exist_ok=True)
    input_filename = file.filename or "audio.bin"
    raw_path = job_in_dir / input_filename
    with raw_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

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
    r.rpush(QUEUE_KEY, job_id)

    try:
        pg_insert_job(job_id, customer, project, language, max_speakers, input_filename)
    except Exception as e:
        print(f"[api] PG insert warning: {e}", flush=True)

    return {
        "job_id":        job_id,
        "status":        "PENDING",
        "customer":      customer,
        "project":       project,
        "output_format": output_format,
    }


# ── Routes — get job ──────────────────────────────────────────────────────────

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

    if status in ("COMPLETED", "DELETED"):
        pg_row = pg_get_job(job_id)
        payload["output_json_url"] = (pg_row or {}).get("output_json_url") or r.hget(k, "output_json_url") or ""
        payload["output_txt_url"]  = r.hget(k, "output_txt_url") or ""
        payload["audio_url"]       = r.hget(k, "audio_url")      or ""
        payload["output_format"]   = r.hget(k, "output_format")  or "both"
        payload["finished_at"]     = (pg_row or {}).get("finished_at") or ""

    return payload


# ── Routes — pause ────────────────────────────────────────────────────────────

@app.post("/jobs/{job_id}/pause")
def pause_job(job_id: str):
    """
    Segnala al worker di fermarsi impostando stop_requested=1.
    Il worker controlla questo flag nel loop di trascrizione e diarizzazione
    e mette il job in PAUSED preservando il file audio su disco.
    """
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or ""
    if status not in ("PENDING", "WORKING"):
        return JSONResponse(status_code=409,
                            content={"error": "not_pausable",
                                     "detail": f"job in stato {status} non può essere sospeso"})

    r.hset(k, mapping={
        "stop_requested": "1",
        "updated_at":     utc_now(),
    })
    # Se era ancora PENDING (non ancora preso dal worker) lo mettiamo direttamente PAUSED
    if status == "PENDING":
        r.hset(k, mapping={
            "status": "PAUSED",
            "step":   "PAUSED_BEFORE_START",
        })
        pg_update_job(job_id, status="PAUSED")

    return {"job_id": job_id, "stop_requested": True, "current_status": status}


# ── Routes — resume ───────────────────────────────────────────────────────────

@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str):
    """
    Riprende un job PAUSED: verifica che il file audio sia ancora su disco,
    poi setta status=PENDING e re-accoda.
    Ritorna 410 Gone se il file audio è stato rimosso dal cleanup.
    """
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or ""
    if status != "PAUSED":
        return JSONResponse(status_code=409,
                            content={"error": "not_paused",
                                     "detail": f"job in stato {status}, solo i job PAUSED possono essere ripresi"})

    input_file = r.hget(k, "input_file") or ""
    if not input_file or not Path(input_file).exists():
        return JSONResponse(status_code=410,
                            content={"error": "audio_missing",
                                     "detail": "file audio rimosso dal cleanup — ricarica il file con un nuovo job"})

    r.hset(k, mapping={
        "status":         "PENDING",
        "step":           "RESUMED",
        "stop_requested": "0",
        "updated_at":     utc_now(),
    })
    r.rpush(QUEUE_KEY, job_id)
    pg_update_job(job_id, status="PENDING")

    return {"job_id": job_id, "status": "PENDING", "step": "RESUMED"}


# ── Routes — retry ────────────────────────────────────────────────────────────

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


# ── Routes — delete ───────────────────────────────────────────────────────────

@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, scope: str = Query(...)):
    """
    Elimina le risorse di un job in base allo scope:

    audio      → file audio originale da MinIO (audio_url) + cartella disco IN
    transcript → scope audio + result.json + result.txt da MinIO + cartella disco OUT
    rag        → scope transcript + Qdrant points + speaker_aliases + minutes_jobs
                 + rag_index_jobs + minute .md da bucket minutes
                 → status=DELETED (soft delete, riga PG e Redis mantengono i metadata)
    purge      → solo su job già DELETED: rimuove wx:job:{id} da Redis
                 + DELETE FROM wx_transcription_jobs (cascade su tutte le tabelle figlie)
    """
    if scope not in ("audio", "transcript", "rag", "purge"):
        return JSONResponse(status_code=400,
                            content={"error": "invalid_scope",
                                     "detail": "scope deve essere: audio | transcript | rag | purge"})

    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404,
                            content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or ""

    # purge è consentito solo su job già DELETED
    if scope == "purge" and status != "DELETED":
        return JSONResponse(status_code=409,
                            content={"error": "not_deleted",
                                     "detail": f"purge richiede status=DELETED, job è {status}. Usa prima scope=rag."})

    # Blocca delete su job attivi
    if scope != "purge" and status in ("WORKING",):
        return JSONResponse(status_code=409,
                            content={"error": "job_active",
                                     "detail": "non puoi eliminare un job in stato WORKING — sospendilo prima"})

    s3  = s3_client()
    ops = []   # log delle operazioni eseguite

    # ── Leggi gli URL da Redis ────────────────────────────────────────────────
    audio_url    = r.hget(k, "audio_url")       or ""
    json_url     = r.hget(k, "output_json_url") or ""
    txt_url      = r.hget(k, "output_txt_url")  or ""

    # ── scope: audio ─────────────────────────────────────────────────────────
    if scope in ("audio", "transcript", "rag"):
        audio_key = minio_key_from_url(audio_url)
        if audio_key:
            minio_delete_key(s3, MINIO_BUCKET, audio_key)
            ops.append(f"MinIO deleted: {MINIO_BUCKET}/{audio_key}")

        # Disco locale — cartella input
        in_dir = IN_DIR / job_id
        if in_dir.exists():
            shutil.rmtree(in_dir, ignore_errors=True)
            ops.append(f"disk deleted: {in_dir}")

        # Aggiorna Redis
        r.hset(k, mapping={"audio_url": "", "updated_at": utc_now()})
        # Aggiorna PG
        pg_update_job(job_id, audio_url=None)

    # ── scope: transcript (aggiuntivo rispetto ad audio) ─────────────────────
    if scope in ("transcript", "rag"):
        json_key = minio_key_from_url(json_url)
        txt_key  = minio_key_from_url(txt_url)

        if json_key:
            minio_delete_key(s3, MINIO_BUCKET, json_key)
            ops.append(f"MinIO deleted: {MINIO_BUCKET}/{json_key}")
        if txt_key:
            minio_delete_key(s3, MINIO_BUCKET, txt_key)
            ops.append(f"MinIO deleted: {MINIO_BUCKET}/{txt_key}")

        # Disco locale — cartella output
        out_dir = OUT_DIR / job_id
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
            ops.append(f"disk deleted: {out_dir}")

        # Aggiorna Redis
        r.hset(k, mapping={
            "output_json_url": "",
            "output_txt_url":  "",
            "updated_at":      utc_now(),
        })
        # Aggiorna PG
        pg_update_job(job_id, output_json_url=None, output_txt_url=None)

    # ── scope: rag (aggiuntivo rispetto a transcript) ─────────────────────────
    if scope == "rag":

        # 1. Qdrant — elimina tutti i punti del job
        qdrant_delete_by_job(job_id)
        ops.append(f"Qdrant deleted points for job_id={job_id}")

        # 2. MinIO bucket minutes — minute .md generate per questo job
        #    Le minute sono salvate sotto: job_id/ nel bucket minutes
        minio_delete_folder(s3, MINUTES_BUCKET, f"{job_id}/")
        ops.append(f"MinIO deleted: {MINUTES_BUCKET}/{job_id}/")

        # 3. PostgreSQL — cancella speaker_aliases, minutes_jobs, rag_index_jobs
        #    (hanno FK con ON DELETE CASCADE, ma qui li cancelliamo esplicitamente
        #     per non fare DROP della riga padre wx_transcription_jobs)
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM speaker_aliases  WHERE job_id = %s", (job_id,))
                cur.execute("DELETE FROM minutes_jobs     WHERE job_id = %s", (job_id,))
                cur.execute("DELETE FROM rag_index_jobs   WHERE job_id = %s", (job_id,))
            ops.append("PG deleted: speaker_aliases, minutes_jobs, rag_index_jobs")
        except Exception as e:
            print(f"[api] PG rag delete warning job {job_id}: {e}", flush=True)
            ops.append(f"PG delete warning: {e}")

        # 4. Soft delete — aggiorna status=DELETED in Redis e PG
        r.hset(k, mapping={"status": "DELETED", "updated_at": utc_now()})
        pg_update_job(job_id, status="DELETED")
        ops.append("status → DELETED")

    # ── scope: purge ──────────────────────────────────────────────────────────
    if scope == "purge":
        # Rimuove la chiave Redis
        r.delete(k)
        ops.append(f"Redis deleted: {k}")

        # Rimuove la riga PG (cascade su speaker_aliases, minutes_jobs, rag_index_jobs)
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM wx_transcription_jobs WHERE job_id = %s", (job_id,))
            ops.append("PG deleted: wx_transcription_jobs (cascade)")
        except Exception as e:
            print(f"[api] PG purge warning job {job_id}: {e}", flush=True)
            ops.append(f"PG purge warning: {e}")

    return {"job_id": job_id, "scope": scope, "ops": ops}