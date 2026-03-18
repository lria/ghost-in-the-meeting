"""
sse_broker.py — Server-Sent Events broker per MinutesAI
Legge lo stato dei job da Redis (scritto da wx_api + wx_worker)
e lo pubblica come event stream al browser.

Endpoints:
  GET /health
  GET /jobs          → lista tutti i job (snapshot da Redis)
  GET /events        → SSE stream — query param: ?jobs=id1,id2,id3
                       oppure ?all=1 per tutti i job non completati
"""

import os
import json
import asyncio
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL     = os.getenv("REDIS_URL",      "redis://redis:6379/0")
POLL_INTERVAL = float(os.getenv("SSE_POLL_INTERVAL", "2.0"))   # secondi tra i poll
KEEPALIVE_SEC = float(os.getenv("SSE_KEEPALIVE_SEC",  "15.0"))  # heartbeat SSE
JOB_KEY_PREFIX = "wx:job:"

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "DELETED"}
# Job da monitorare attivamente: tutto ciò che non è terminale
# PAUSED è incluso — il worker potrebbe riprenderlo e farlo tornare WORKING
ACTIVE_STATUSES_WATCH = lambda s: s.upper() not in TERMINAL_STATUSES


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="MinutesAI SSE Broker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Redis pool (condiviso tra le richieste) ───────────────────────────────────

_redis: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis

# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

async def fetch_job(r: aioredis.Redis, job_id: str) -> dict | None:
    """Legge tutti i campi dell'hash wx:job:{job_id} da Redis."""
    key = f"{JOB_KEY_PREFIX}{job_id}"
    data = await r.hgetall(key)
    if not data:
        return None
    return {
        "job_id":        data.get("job_id", job_id),
        "status":        data.get("status", "UNKNOWN"),
        "step":          data.get("step", ""),
        "customer":      data.get("customer", ""),
        "project":       data.get("project", ""),
        "language":      data.get("language", ""),
        "output_format": data.get("output_format", ""),
        "created_at":    data.get("created_at", ""),
        "updated_at":    data.get("updated_at", ""),
        "output_json_url": data.get("output_json_url", ""),
        "output_txt_url":  data.get("output_txt_url", ""),
        "audio_url":       data.get("audio_url", ""),
        "error":           data.get("error", ""),
    }

async def fetch_all_jobs(r: aioredis.Redis) -> list[dict]:
    """Scansiona tutti i wx:job:* e restituisce la lista ordinata per created_at."""
    jobs = []
    async for key in r.scan_iter(match=f"{JOB_KEY_PREFIX}*", count=100):
        job_id = key.removeprefix(JOB_KEY_PREFIX)
        job = await fetch_job(r, job_id)
        if job:
            jobs.append(job)
    # Ordina per created_at decrescente (i più recenti prima)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs

def sse_event(event: str, data: dict) -> str:
    """Formatta un evento SSE."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

def sse_comment(msg: str) -> str:
    """Heartbeat SSE (i commenti mantengono la connessione aperta)."""
    return f": {msg}\n\n"

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    r = await get_redis()
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"ok": True, "redis": redis_ok, "poll_interval": POLL_INTERVAL}


@app.get("/jobs")
async def list_jobs(
    status: str = Query(None, description="Filtra per status (es. PENDING,WORKING)"),
    limit:  int = Query(100,  description="Numero massimo di job restituiti"),
):
    """
    Snapshot di tutti i job su Redis.
    Usato dal browser al caricamento pagina.
    """
    r = await get_redis()
    jobs = await fetch_all_jobs(r)

    if status:
        allowed = {s.strip().upper() for s in status.split(",")}
        jobs = [j for j in jobs if j["status"].upper() in allowed]

    return {"jobs": jobs[:limit], "total": len(jobs), "snapshot_at": utc_now()}


@app.get("/events")
async def event_stream(
    jobs: str = Query(None, description="job_id separati da virgola da monitorare"),
    all:  int = Query(0,    description="Se 1, monitora tutti i job non terminali"),
):
    """
    SSE stream.
    - Invia subito uno snapshot iniziale per ogni job richiesto.
    - Poi fa poll ogni POLL_INTERVAL secondi e manda solo i delta (cambiamenti di stato/step).
    - Quando tutti i job monitorati sono terminali, manda 'stream_end' e chiude.
    """
    async def generate() -> AsyncGenerator[str, None]:
        r = await get_redis()

        # Determina i job_id da monitorare
        if all:
            all_jobs = await fetch_all_jobs(r)
            watch_ids = [
                j["job_id"] for j in all_jobs
                if ACTIVE_STATUSES_WATCH(j["status"])
            ]
        elif jobs:
            watch_ids = [jid.strip() for jid in jobs.split(",") if jid.strip()]
        else:
            yield sse_comment("no jobs requested")
            return

        if not all and not watch_ids:
            yield sse_event("stream_end", {"message": "nessun job attivo"})
            return

        # Snapshot iniziale per tutti i job richiesti
        prev_states: dict[str, dict] = {}
        for job_id in watch_ids:
            job = await fetch_job(r, job_id)
            if job:
                prev_states[job_id] = job
                yield sse_event("job_update", job)
            else:
                yield sse_event("job_not_found", {"job_id": job_id})

        # Stream loop
        last_keepalive = asyncio.get_event_loop().time()
        last_rescan    = asyncio.get_event_loop().time()
        RESCAN_SEC     = 10.0  # ogni 10s controlla se ci sono nuovi job non-terminali

        while True:
            await asyncio.sleep(POLL_INTERVAL)

            now = asyncio.get_event_loop().time()

            # Heartbeat
            if now - last_keepalive >= KEEPALIVE_SEC:
                yield sse_comment(f"keepalive {utc_now()}")
                last_keepalive = now

            # Rescan periodico: aggiunge job nuovi o RESUMED non ancora in watch_ids
            if all and now - last_rescan >= RESCAN_SEC:
                fresh_jobs = await fetch_all_jobs(r)
                for fj in fresh_jobs:
                    fid = fj["job_id"]
                    if fid not in watch_ids and ACTIVE_STATUSES_WATCH(fj["status"]):
                        watch_ids.append(fid)
                        prev_states[fid] = fj
                        yield sse_event("job_update", fj)
                last_rescan = now

            if not watch_ids:
                yield sse_comment("idle — no active jobs")
                continue

            completed_this_round = []

            for job_id in list(watch_ids):
                job = await fetch_job(r, job_id)
                if not job:
                    continue

                prev = prev_states.get(job_id, {})

                # Manda evento solo se qualcosa è cambiato
                if job.get("status") != prev.get("status") or job.get("step") != prev.get("step"):
                    prev_states[job_id] = job
                    yield sse_event("job_update", job)

                # Rimuovi dalla watch list se terminale
                if job["status"].upper() in TERMINAL_STATUSES:
                    completed_this_round.append(job_id)

            for job_id in completed_this_round:
                watch_ids.remove(job_id)

            # Per ?jobs=... specifici: chiudi quando tutti terminali
            if not all and not watch_ids:
                yield sse_event("stream_end", {"message": "tutti i job monitorati sono terminali", "at": utc_now()})
                return

        # (stream ?all=1 non termina mai — il browser si disconnette quando vuole)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disabilita buffering nginx
            "Connection":       "keep-alive",
        },
    )