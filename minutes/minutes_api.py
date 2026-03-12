"""
minutes_api.py — Meeting Minutes API (AI)
Porta: 9600

Endpoints:
  POST /minutes/jobs           → crea job generazione minuta
  GET  /minutes/jobs/{id}      → polling stato + preview
  POST /minutes/jobs/{id}/retry
  GET  /minutes/models         → lista modelli Ollama disponibili
  GET  /minutes/templates      → lista template disponibili
  GET  /health
"""

import os
import uuid
import json
import psycopg2
import requests
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import redis

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL    = os.getenv("REDIS_URL",           "redis://redis:6379/0")
QUEUE_KEY    = os.getenv("MINUTES_QUEUE_KEY",   "minutes:queue")
OLLAMA_URL   = os.getenv("OLLAMA_URL",          "http://ollama:11434")

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

TEMPLATES_DIR = Path(os.getenv("MINUTES_TEMPLATES_DIR", "/app/templates"))

def _parse_template_header(path: Path) -> dict:
    """Legge solo l'header (fino a '---') di un file template."""
    meta = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() == "---":
                break
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
    except Exception:
        pass
    # Tags: "verbale, formale, decisioni" → ["verbale", "formale", "decisioni"]
    tags_raw = meta.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    return {
        "key":         path.stem,
        "name":        meta.get("title",       path.stem),
        "tags":        tags,
        "description": meta.get("description", ""),
    }

def discover_templates_meta() -> dict[str, dict]:
    if not TEMPLATES_DIR.exists():
        print(f"[minutes_api] ⚠ TEMPLATES_DIR non trovata: {TEMPLATES_DIR}", flush=True)
        return _fallback_templates()
    result = {}
    for path in sorted(TEMPLATES_DIR.glob("*.txt")):
        tpl = _parse_template_header(path)
        result[tpl["key"]] = tpl
        print(f"[minutes_api] template: {tpl['key']} → {tpl['name']}", flush=True)
    if not result:
        print(f"[minutes_api] ⚠ nessun .txt in {TEMPLATES_DIR} — uso fallback", flush=True)
        return _fallback_templates()
    return result

def _fallback_templates() -> dict[str, dict]:
    """Template di emergenza hardcoded — attivi solo se la cartella è vuota o mancante."""
    return {
        "standard": {
            "key": "standard",
            "name": "Verbale Standard",
            "tags": ["verbale", "formale", "decisioni", "azioni"],
            "description": "Verbale formale con sezioni fisse: oggetto, punti discussi, decisioni, azioni, prossimi passi",
        },
        "client_meeting": {
            "key": "client_meeting",
            "name": "Meeting Cliente",
            "tags": ["cliente", "enterprise", "PM", "rischi", "tecnico"],
            "description": "Formato PM senior per riunioni enterprise: tipo meeting, decisioni, rischi, note tecniche",
        },
    }

CONTEXT_LEVELS_META = {
    "standalone":       {"label": "Solo questa riunione",              "description": "Nessun contesto storico — minuta stand-alone"},
    "customer":         {"label": "Contesto cliente",                  "description": "Arricchisce con tutte le riunioni dello stesso cliente"},
    "customer_project": {"label": "Contesto cliente + progetto",       "description": "Usa riunioni precedenti dello stesso progetto"},
    "speakers":         {"label": "Contesto per speaker",              "description": "Recupera interventi storici dei partecipanti"},
}

# ── Clients ───────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
app = FastAPI(title="Minutes AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def minutes_key(minutes_id: str) -> str:
    return f"minutes:job:{minutes_id}"


def pg_conn():
    return psycopg2.connect(PG_DSN)


def pg_insert_minutes(minutes_id: str, job_id: str, template: str,
                       context_level: str, model: str, language: str,
                       customer: str, project: str, participants: str):
    sql = """
        INSERT INTO minutes_jobs
          (minutes_id, job_id, status, step, template, context_level,
           model, language, customer, project, participants, created_at, updated_at)
        VALUES (%s,%s,'PENDING','ENQUEUED',%s,%s,%s,%s,%s,%s,%s,now(),now())
        ON CONFLICT (minutes_id) DO NOTHING
    """
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (minutes_id, job_id, template, context_level,
                              model, language, customer, project, participants))
    except Exception as e:
        print(f"[minutes_api] PG insert warning: {e}", flush=True)


def pg_get_minutes(minutes_id: str) -> dict | None:
    sql = """
        SELECT minutes_id, job_id, status, step, template, context_level,
               model, language, customer, project,
               output_url, error_message,
               created_at, updated_at, started_at, finished_at
        FROM minutes_jobs WHERE minutes_id = %s
    """
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (minutes_id,))
            row = cur.fetchone()
        if not row:
            return None
        cols = ["minutes_id","job_id","status","step","template","context_level",
                "model","language","customer","project",
                "output_url","error_message",
                "created_at","updated_at","started_at","finished_at"]
        return {c: (v.isoformat() if hasattr(v, "isoformat") else v)
                for c, v in zip(cols, row)}
    except Exception as e:
        print(f"[minutes_api] pg_get_minutes warning: {e}", flush=True)
        return None


def get_source_urls(job_id: str) -> dict:
    """Legge i file sorgente della trascrizione da Redis wx:job:{job_id}."""
    wx_key = f"wx:job:{job_id}"
    return {
        "source_json_url": r.hget(wx_key, "output_json_url") or None,
        "source_txt_url":  r.hget(wx_key, "output_txt_url")  or None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    queue_len = r.llen(QUEUE_KEY)
    ollama_ok = False
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        ollama_ok = resp.ok
    except Exception:
        pass
    return {"ok": True, "queue": QUEUE_KEY, "queued_jobs": queue_len, "ollama": ollama_ok}


@app.get("/minutes/templates")
def list_templates():
    return {"templates": discover_templates_meta()}


@app.get("/minutes/context-levels")
def list_context_levels():
    return {"context_levels": CONTEXT_LEVELS_META}


@app.get("/minutes/models")
def list_models():
    """
    Lista i modelli disponibili su Ollama.
    Restituisce anche suggerimenti per modelli non ancora installati.
    """
    available = []
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if resp.ok:
            available = [
                {
                    "name":    m["name"],
                    "size_gb": round(m.get("size", 0) / 1e9, 1),
                    "modified": m.get("modified_at", ""),
                }
                for m in resp.json().get("models", [])
            ]
    except Exception as e:
        print(f"[minutes_api] Ollama non raggiungibile: {e}", flush=True)

    suggested = [
        {"name": "gemma3:12b",    "size_gb": 8.1,  "note": "Ottimo italiano, context 128k — consigliato"},
        {"name": "mistral:7b",    "size_gb": 4.1,  "note": "Veloce, buon italiano"},
        {"name": "qwen2.5:7b",    "size_gb": 4.4,  "note": "Ottimo per output strutturati"},
        {"name": "llama3.2:3b",   "size_gb": 2.0,  "note": "Leggero, per test"},
    ]

    # Marca come installati i suggeriti già disponibili
    available_names = {m["name"] for m in available}
    for s in suggested:
        s["installed"] = s["name"] in available_names

    return {
        "available": available,
        "suggested": suggested,
        "ollama_reachable": len(available) >= 0,
    }


@app.post("/minutes/models/pull", status_code=202)
async def pull_model(model: str = Form(...)):
    """
    Avvia ollama pull in background.
    Ritorna 202 subito; il client fa polling su GET /minutes/models
    per sapere quando il modello è disponibile.
    """
    import threading

    def _do_pull(model_name: str):
        try:
            print(f"[minutes_api] pull '{model_name}' avviato", flush=True)
            resp = requests.post(
                f"{OLLAMA_URL}/api/pull",
                json={"name": model_name, "stream": False},
                timeout=900,
            )
            print(f"[minutes_api] pull '{model_name}' completato — status {resp.status_code}", flush=True)
        except Exception as e:
            print(f"[minutes_api] pull '{model_name}' errore: {e}", flush=True)

    threading.Thread(target=_do_pull, args=(model,), daemon=True).start()
    return {"status": "pulling", "model": model}


@app.post("/minutes/jobs", status_code=202)
async def create_minutes_job(
    job_id:        str  = Form(...,           description="ID del job di trascrizione (wx_transcription_jobs)"),
    template:      str  = Form("standard",    description="Chiave template (nome file senza .txt)"),
    context_level: str  = Form("standalone",  description="standalone | customer | customer_project | speakers"),
    model:         str  = Form(...,           description="Nome modello Ollama, es. mistral:7b"),
    participants:  str  = Form("[]",          description="JSON array [{speaker, name, role}]"),
):
    # Validazioni
    available_templates = discover_templates_meta()
    if template not in available_templates:
        raise HTTPException(
            status_code=400,
            detail=f"template '{template}' non trovato. Disponibili: {list(available_templates)}"
        )
    if context_level not in CONTEXT_LEVELS_META:
        raise HTTPException(status_code=400, detail=f"context_level non valido: {context_level}")

    # Leggi metadati dal job di trascrizione
    wx_key = f"wx:job:{job_id}"
    if not r.exists(wx_key):
        raise HTTPException(status_code=404, detail=f"job trascrizione non trovato: {job_id}")

    wx_status = r.hget(wx_key, "status") or ""
    if wx_status != "COMPLETED":
        raise HTTPException(
            status_code=400,
            detail=f"job trascrizione non completato (status: {wx_status})"
        )

    customer = r.hget(wx_key, "customer") or ""
    project  = r.hget(wx_key, "project")  or ""
    language = r.hget(wx_key, "language") or "it"

    minutes_id = uuid.uuid4().hex

    # Guard: se esiste già un job PENDING o WORKING per (job_id, template) → 409
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT minutes_id FROM minutes_jobs WHERE job_id = %s AND template = %s "
                "AND status IN ('PENDING','WORKING') LIMIT 1",
                (job_id, template)
            )
            existing = cur.fetchone()
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_active",
                    "minutes_id": existing[0],
                    "message": f"Esiste già un job attivo per template '{template}'",
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[minutes_api] guard check warning: {e}", flush=True)

    meta = {
        "minutes_id":    minutes_id,
        "job_id":        job_id,
        "status":        "PENDING",
        "step":          "ENQUEUED",
        "template":      template,
        "context_level": context_level,
        "model":         model,
        "language":      language,
        "customer":      customer,
        "project":       project,
        "participants":  participants,
        "created_at":    utc_now(),
        "updated_at":    utc_now(),
    }
    r.hset(minutes_key(minutes_id), mapping=meta)
    r.rpush(QUEUE_KEY, minutes_id)

    pg_insert_minutes(minutes_id, job_id, template, context_level,
                      model, language, customer, project, participants)

    return {
        "minutes_id":    minutes_id,
        "status":        "PENDING",
        "customer":      customer,
        "project":       project,
        "template":      template,
        "context_level": context_level,
        "model":         model,
    }


@app.get("/minutes/jobs/{minutes_id}")
def get_minutes_job(minutes_id: str):
    k = minutes_key(minutes_id)
    if not r.exists(k):
        raise HTTPException(status_code=404, detail="minutes job non trovato")

    status = r.hget(k, "status") or "UNKNOWN"

    payload = {
        "minutes_id":    minutes_id,
        "job_id":        r.hget(k, "job_id")        or "",
        "status":        status,
        "step":          r.hget(k, "step")          or "",
        "template":      r.hget(k, "template")      or "",
        "context_level": r.hget(k, "context_level") or "",
        "model":         r.hget(k, "model")         or "",
        "customer":      r.hget(k, "customer")      or "",
        "project":       r.hget(k, "project")       or "",
        "created_at":    r.hget(k, "created_at")    or "",
        "updated_at":    r.hget(k, "updated_at")    or "",
    }

    if status == "COMPLETED":
        output_url = r.hget(k, "output_url") or ""
        pg_row = pg_get_minutes(minutes_id)
        if pg_row and not output_url:
            output_url = pg_row.get("output_url") or ""

        payload["output_url"]  = output_url
        payload["preview"]     = r.hget(k, "preview") or ""
        payload["finished_at"] = (pg_row or {}).get("finished_at", "")
        payload.update(get_source_urls(payload["job_id"]))

    if status == "FAILED":
        payload["error"] = r.hget(k, "error") or "errore sconosciuto"

    return payload


@app.get("/minutes/jobs")
def list_minutes_jobs(
    job_id:   str | None = None,
    customer: str | None = None,
    limit:    int = 20,
):
    """Lista tutti i minutes jobs, opzionalmente filtrati per job_id o customer."""
    results = []
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            if job_id:
                cur.execute(
                    "SELECT minutes_id, job_id, status, step, template, context_level, model, "
                    "customer, project, output_url, created_at, finished_at "
                    "FROM minutes_jobs WHERE job_id = %s ORDER BY created_at DESC LIMIT %s",
                    (job_id, limit)
                )
            elif customer:
                cur.execute(
                    "SELECT minutes_id, job_id, status, step, template, context_level, model, "
                    "customer, project, output_url, created_at, finished_at "
                    "FROM minutes_jobs WHERE customer = %s ORDER BY created_at DESC LIMIT %s",
                    (customer, limit)
                )
            else:
                cur.execute(
                    "SELECT minutes_id, job_id, status, step, template, context_level, model, "
                    "customer, project, output_url, created_at, finished_at "
                    "FROM minutes_jobs ORDER BY created_at DESC LIMIT %s",
                    (limit,)
                )
            cols = ["minutes_id","job_id","status","step","template","context_level","model",
                    "customer","project","output_url","created_at","finished_at"]
            for row in cur.fetchall():
                results.append({c: (v.isoformat() if hasattr(v, "isoformat") else v)
                                 for c, v in zip(cols, row)})
    except Exception as e:
        print(f"[minutes_api] list warning: {e}", flush=True)

    return {"jobs": results, "total": len(results)}


@app.post("/minutes/jobs/{minutes_id}/retry")
def retry_minutes_job(minutes_id: str):
    k = minutes_key(minutes_id)
    if not r.exists(k):
        raise HTTPException(status_code=404, detail="minutes job non trovato")

    r.hset(k, mapping={
        "status":     "PENDING",
        "step":       "REQUEUED_MANUAL",
        "error":      "",
        "updated_at": utc_now(),
    })
    r.rpush(QUEUE_KEY, minutes_id)

    return {"minutes_id": minutes_id, "status": "PENDING", "step": "REQUEUED_MANUAL"}


@app.delete("/minutes/jobs/{minutes_id}", status_code=200)
def delete_minutes_job(minutes_id: str):
    """Elimina un job minuta da Redis e PostgreSQL. Non tocca il file su MinIO."""
    k = minutes_key(minutes_id)
    # Leggi job_id prima di cancellare (per pulire minutes_url su wx:job)
    job_id = r.hget(k, "job_id") or ""
    r.delete(k)

    # Rimuovi minutes_url da wx:job se questo era l'ultimo job minutes per quel job_id
    if job_id:
        r.hdel(f"wx:job:{job_id}", "minutes_url")

    # Pulizia PostgreSQL
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM minutes_jobs WHERE minutes_id = %s", (minutes_id,))
    except Exception as e:
        print(f"[api] PG delete warning: {e}", flush=True)

    return {"minutes_id": minutes_id, "deleted": True}