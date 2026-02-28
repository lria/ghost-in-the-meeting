# /app/async_routes.py
import json
import os
import tempfile
import threading
import uuid
from typing import Optional, Dict, Any

import redis
import requests
import boto3
from botocore.exceptions import ClientError

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

# -----------------------------
# Config
# -----------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
MINIO_REGION = os.getenv("MINIO_REGION", "us-east-1")

JOB_PREFIX = os.getenv("WHISPERX_JOB_PREFIX", "wxjob:")
TMP_DIR = os.getenv("WHISPERX_TMP_DIR", "/app/data")

# -----------------------------
# Clients
# -----------------------------
_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


def _s3():
    # endpoint_url può essere http://minio:9000 o https://...
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name=MINIO_REGION,
        verify=MINIO_SECURE,  # se MINIO_SECURE=false -> verify=False
    )


def _job_key(job_id: str) -> str:
    return f"{JOB_PREFIX}{job_id}"


def _set_job(job_id: str, payload: Dict[str, Any]) -> None:
    _r.set(_job_key(job_id), json.dumps(payload, ensure_ascii=False))


def _get_job(job_id: str) -> Dict[str, Any]:
    raw = _r.get(_job_key(job_id))
    if not raw:
        raise KeyError(job_id)
    return json.loads(raw)


def _ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        # MinIO spesso risponde con 404/NoSuchBucket
        if code in ("404", "NoSuchBucket", "NotFound"):
            s3.create_bucket(Bucket=bucket)
        else:
            raise


def _compute_output_key(output_key: Optional[str], output_prefix: Optional[str]) -> str:
    if output_key and output_key.strip():
        return output_key.strip().lstrip("/")
    if output_prefix and output_prefix.strip():
        p = output_prefix.strip().strip("/")
        return f"{p}/transcript.json"
    # fallback (evita null)
    return "transcript.json"


def _run_job(job_id: str, local_path: str, query: str, output_bucket: str, out_key: str) -> None:
    job = _get_job(job_id)
    job["state"] = "running"
    _set_job(job_id, job)

    try:
        # 1) chiama sync endpoint nel container
        url = f"http://127.0.0.1:8000/transcription/?{query.lstrip('?')}"
        with open(local_path, "rb") as f:
            files = {"file": (os.path.basename(local_path), f, "application/octet-stream")}
            resp = requests.post(url, files=files, timeout=None)

        if resp.status_code >= 400:
            raise RuntimeError(f"sync transcription failed {resp.status_code}: {resp.text[:800]}")

        # 2) salva su MinIO
        s3 = _s3()
        _ensure_bucket(s3, output_bucket)
        s3.put_object(
            Bucket=output_bucket,
            Key=out_key,
            Body=resp.content,
            ContentType="application/json",
        )

        job = _get_job(job_id)
        job["state"] = "done"
        job["output"] = {"bucket": output_bucket, "key": out_key}
        _set_job(job_id, job)

    except Exception as e:
        job = _get_job(job_id)
        job["state"] = "error"
        job["error"] = str(e)
        _set_job(job_id, job)

    finally:
        try:
            os.remove(local_path)
        except Exception:
            pass


@router.post("/transcription_async")
async def transcription_async(
    query: str = Form(...),  # es: "language=it&model_size=tiny&device=cpu&diarize=false&compute_type=int8"
    file: UploadFile = File(...),
    output_bucket: str = Form(...),
    output_key: Optional[str] = Form(None),
    output_prefix: Optional[str] = Form(None),
):
    job_id = uuid.uuid4().hex
    out_key = _compute_output_key(output_key, output_prefix)

    os.makedirs(TMP_DIR, exist_ok=True)

    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, local_path = tempfile.mkstemp(prefix=f"wx_{job_id}_", suffix=suffix, dir=TMP_DIR)
    os.close(fd)

    # salva subito il file locale (così la response non aspetta)
    with open(local_path, "wb") as f:
        f.write(await file.read())

    _set_job(job_id, {
        "job_id": job_id,
        "state": "queued",
        "output": {"bucket": output_bucket, "key": out_key},
    })

    t = threading.Thread(
        target=_run_job,
        args=(job_id, local_path, query, output_bucket, out_key),
        daemon=True,
    )
    t.start()

    return JSONResponse(status_code=202, content={"job_id": job_id, "state": "queued"})


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    try:
        return _get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")