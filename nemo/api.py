import os
import uuid
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Query
from fastapi.responses import JSONResponse

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_KEY = os.getenv("NEMO_QUEUE_KEY", "nemo:queue")

DATA_DIR = Path(os.getenv("NEMO_DATA_DIR", "/data"))
IN_DIR = DATA_DIR / "in"
OUT_DIR = DATA_DIR / "out"
IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="NeMo Jobs API (async polling)")

def job_key(job_id: str) -> str:
    return f"nemo:job:{job_id}"

@app.get("/health")
def health():
    return {"ok": True, "redis": REDIS_URL, "queue": QUEUE_KEY}

@app.post("/jobs")
async def submit_job(
    file: UploadFile = File(...),
    max_speakers: int = Query(8, ge=1, le=20),
):
    job_id = uuid.uuid4().hex
    jobdir = IN_DIR / job_id
    jobdir.mkdir(parents=True, exist_ok=True)

    raw_path = jobdir / (file.filename or "audio")
    with raw_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    meta = {
        "job_id": job_id,
        "status": "PENDING",
        "max_speakers": int(max_speakers),
        "input_file": str(raw_path),
    }

    r.hset(job_key(job_id), mapping=meta)
    # enqueue job id
    r.rpush(QUEUE_KEY, job_id)

    return {"job_id": job_id, "status": "PENDING"}

@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    k = job_key(job_id)
    if not r.exists(k):
        return JSONResponse(status_code=404, content={"error": "job_not_found", "job_id": job_id})

    status = r.hget(k, "status") or "UNKNOWN"
    res_path = r.hget(k, "result_path")

    payload = {"job_id": job_id, "status": status}

    if status == "FAILED":
        payload["error"] = r.hget(k, "error") or "unknown_error"
        payload["detail"] = r.hget(k, "detail") or ""

    if status == "COMPLETED" and res_path and Path(res_path).exists():
        try:
            payload["result"] = json.loads(Path(res_path).read_text(encoding="utf-8"))
        except Exception as e:
            payload["status"] = "FAILED"
            payload["error"] = "result_read_failed"
            payload["detail"] = str(e)

    return payload