import os, json, uuid, tempfile
from typing import Optional

import httpx
import redis
from minio import Minio
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

# Importa la FastAPI app già esistente del container (di solito è "app" in /app/main.py)
from main import app as app  # noqa: F401


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
m = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=MINIO_SECURE,
)

JOB_PREFIX = "wxjob:"


def _job_key(job_id: str) -> str:
    return f"{JOB_PREFIX}{job_id}"


def _set_job(job_id: str, payload: dict):
    r.set(_job_key(job_id), json.dumps(payload))


def _get_job(job_id: str) -> dict:
    raw = r.get(_job_key(job_id))
    if not raw:
        raise KeyError(job_id)
    return json.loads(raw)


async def _run_job(job_id: str, local_path: str, query: str,
                   output_bucket: str, output_key: str,
                   output_txt_key: Optional[str]):
    # stato running
    job = _get_job(job_id)
    job["state"] = "running"
    _set_job(job_id, job)

    try:
        # Richiama l'endpoint sync interno già funzionante
        # NB: usa http://127.0.0.1:8000 perché gira nello stesso container
        url = f"http://127.0.0.1:8000/transcription/?{query}"

        async with httpx.AsyncClient(timeout=None) as client:
            with open(local_path, "rb") as f:
                files = {"file": (os.path.basename(local_path), f, "application/octet-stream")}
                resp = await client.post(url, files=files)

        if resp.status_code >= 400:
            raise RuntimeError(f"whisperx sync error {resp.status_code}: {resp.text[:500]}")

        # Salva JSON su MinIO
        data = resp.content  # bytes JSON
        # garantisci bucket esista
        if not m.bucket_exists(output_bucket):
            m.make_bucket(output_bucket)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            size = os.path.getsize(tmp.name)
            m.fput_object(output_bucket, output_key, tmp.name, content_type="application/json")

        # opzionale: estrai testo e salva txt
        if output_txt_key:
            try:
                j = resp.json()
                # adattalo alla tua response (qui prendo una versione robusta)
                text = j.get("text") or j.get("segments") or j
                txt = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            except Exception:
                txt = resp.text

            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp2:
                tmp2.write(txt)
                tmp2.flush()
                m.fput_object(output_bucket, output_txt_key, tmp2.name, content_type="text/plain")

        job = _get_job(job_id)
        job["state"] = "done"
        job["output"] = {
            "bucket": output_bucket,
            "key": output_key,
            "txt_key": output_txt_key,
        }
        _set_job(job_id, job)

    except Exception as e:
        job = _get_job(job_id)
        job["state"] = "error"
        job["error"] = str(e)
        _set_job(job_id, job)

    finally:
        # cleanup file locale
        try:
            os.remove(local_path)
        except Exception:
            pass


@app.post("/transcription_async")
async def transcription_async(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    output_bucket: str = Form(...),
    output_key: str = Form(...),
    output_txt_key: Optional[str] = Form(None),
):
    job_id = uuid.uuid4().hex

    # salva multipart su disco (così la response torna subito)
    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, local_path = tempfile.mkstemp(prefix=f"wx_{job_id}_", suffix=suffix)
    os.close(fd)

    with open(local_path, "wb") as f:
        f.write(await file.read())

    # prendi query string originale (language, model_size, ecc.)
    # FastAPI qui non la passa automaticamente: la recuperiamo lato client n8n costruendola nella URL.
    # Quindi: n8n deve chiamare /transcription_async?<params> e qui li leggiamo via request scope
    # Per semplicità, li facciamo passare in header.
    # -> soluzione più semplice: aggiungi un Form "query" che contiene la query string.
    # Ma per non cambiare ora, lanciare 400 finché non la implementi correttamente.
    raise HTTPException(status_code=400, detail="Add Form field 'query' with the transcription query string")


@app.post("/transcription_async_v2")
async def transcription_async_v2(
    background: BackgroundTasks,
    query: str = Form(...),  # es: "language=it&model_size=tiny&device=cpu&diarize=false&compute_type=int8"
    file: UploadFile = File(...),
    output_bucket: str = Form(...),
    output_key: str = Form(...),
    output_txt_key: Optional[str] = Form(None),
):
    job_id = uuid.uuid4().hex

    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, local_path = tempfile.mkstemp(prefix=f"wx_{job_id}_", suffix=suffix)
    os.close(fd)

    with open(local_path, "wb") as f:
        f.write(await file.read())

    _set_job(job_id, {
        "job_id": job_id,
        "state": "queued",
        "output": {"bucket": output_bucket, "key": output_key, "txt_key": output_txt_key},
    })

    background.add_task(_run_job, job_id, local_path, query, output_bucket, output_key, output_txt_key)

    return JSONResponse(status_code=202, content={"job_id": job_id, "state": "queued"})


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    try:
        return _get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
