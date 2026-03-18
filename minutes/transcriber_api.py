"""
minutes_api.py — Minutes Generation API
Endpoints:
  GET  /health
  GET  /jobs/{job_id}/speakers          → speaker rilevati + suggerimenti
  POST /jobs/{job_id}/speakers          → salva mapping speaker
  GET  /ollama/models                   → modelli Ollama disponibili
  POST /minutes/generate                → avvia generazione minuta (async)
  GET  /minutes/{minutes_job_id}        → stato + risultato
  GET  /minutes/by-job/{job_id}         → ultima minuta per job
  GET  /rag/search                      → debug query Qdrant
"""

import os
import re
import json
import uuid
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
import redis
import boto3
import psycopg2
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Qdrant ────────────────────────────────────────────────────────────────────
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct, Filter,
        FieldCondition, MatchValue
    )
    QDRANT_OK = True
except ImportError:
    QDRANT_OK = False
    print("[minutes] ⚠ qdrant-client non disponibile", flush=True)

# ── Sentence Transformers ─────────────────────────────────────────────────────
try:
    from sentence_transformers import SentenceTransformer
    _embedder = None  # lazy load
    ST_OK = True
except ImportError:
    ST_OK = False
    print("[minutes] ⚠ sentence-transformers non disponibile", flush=True)

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL      = os.getenv("REDIS_URL",       "redis://redis:6379/0")
OLLAMA_URL     = os.getenv("OLLAMA_URL",       "http://ollama:11434")
QDRANT_URL     = os.getenv("QDRANT_URL",       "http://qdrant:6333")
MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "http://minio:9000")   # boto3 — interno Docker
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", "http://localhost:9000") # URL salvato in DB/Redis
MINIO_ACCESS   = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET   = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET",     "wx-transcriptions")
MINIO_SECURE   = os.getenv("MINIO_SECURE",     "false").lower() == "true"

QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "meeting_minutes")
EMBED_MODEL       = os.getenv("EMBED_MODEL",        "all-MiniLM-L6-v2")
EMBED_DIM         = 384

PROMPTS_DIR = Path(os.getenv("PROMPTS_DIR", "/app/prompts"))

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

# ── Clients ───────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
app = FastAPI(title="Minutes API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def pg():
    return psycopg2.connect(PG_DSN)

def s3():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        region_name="us-east-1",
        verify=MINIO_SECURE,
    )

def get_embedder():
    global _embedder
    if _embedder is None and ST_OK:
        print("[minutes] Caricamento embedder...", flush=True)
        _embedder = SentenceTransformer(EMBED_MODEL)
        print("[minutes] Embedder pronto.", flush=True)
    return _embedder

def qdrant():
    if not QDRANT_OK:
        return None
    return QdrantClient(url=QDRANT_URL)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def job_key(job_id: str) -> str:
    return f"wx:job:{job_id}"

def _parse_minio_url(url: str) -> tuple[str, str]:
    """
    Estrae (bucket, key) da un URL MinIO pubblico o interno.
    Funziona indipendentemente da localhost:9000 vs minio:9000.
    http://localhost:9000/wx-transcriptions/Acme/Q1/result.json
    -> ('wx-transcriptions', 'Acme/Q1/result.json')
    """
    parsed = urlparse(url)
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if len(path_parts) != 2 or not path_parts[0] or not path_parts[1]:
        raise ValueError(f"URL MinIO non valido (atteso /bucket/key): {url}")
    return path_parts[0], path_parts[1]


# ── Qdrant init ───────────────────────────────────────────────────────────────

def ensure_qdrant_collection():
    if not QDRANT_OK:
        return
    try:
        client = qdrant()
        existing = [c.name for c in client.get_collections().collections]
        if QDRANT_COLLECTION not in existing:
            client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
            print(f"[minutes] Qdrant collection '{QDRANT_COLLECTION}' creata.", flush=True)
        else:
            print(f"[minutes] Qdrant collection '{QDRANT_COLLECTION}' già esistente.", flush=True)
    except Exception as e:
        print(f"[minutes] ⚠ Qdrant init warning: {e}", flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_job_from_redis(job_id: str) -> dict:
    k = job_key(job_id)
    if not r.exists(k):
        raise HTTPException(status_code=404, detail=f"job {job_id} non trovato")
    return r.hgetall(k)

def get_job_speakers_from_redis(job_id: str) -> list[str]:
    """Estrae gli speaker unici dal result.json su MinIO."""
    job = get_job_from_redis(job_id)
    output_json_url = job.get("output_json_url", "")
    if not output_json_url:
        return []
    # Estrai bucket e key dall'URL (funziona con localhost:9000 e minio:9000)
    try:
        bucket, key = _parse_minio_url(output_json_url)
        client = s3()
        obj = client.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read())
        speakers = list({seg.get("speaker", "UNKNOWN_x") for seg in data.get("segments", [])})
        known   = sorted([s for s in speakers if not s.startswith("UNKNOWN")])
        unknown = sorted([s for s in speakers if s.startswith("UNKNOWN")])
        return known + unknown
    except Exception as e:
        print(f"[minutes] ⚠ get_job_speakers warning: {e}", flush=True)
        return []

def get_transcript_text(job_id: str) -> tuple[str, list[dict]]:
    """
    Restituisce (testo_plain, segments).
    segments: [{start, end, speaker, text}]
    """
    job = get_job_from_redis(job_id)
    output_json_url = job.get("output_json_url", "")
    if not output_json_url:
        return "", []
    try:
        bucket, key = _parse_minio_url(output_json_url)
        client = s3()
        obj = client.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read())
        segments = data.get("segments", [])
        full_text = data.get("full_text", " ".join(s.get("text","") for s in segments))
        return full_text, segments
    except Exception as e:
        print(f"[minutes] ⚠ get_transcript_text warning: {e}", flush=True)
        return "", []

def get_speaker_aliases(job_id: str) -> dict[str, dict]:
    """Restituisce {speaker_id: {name, company}} per il job."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT speaker_id, name, company FROM speaker_aliases WHERE job_id = %s",
                (job_id,)
            )
            return {row[0]: {"name": row[1], "company": row[2] or ""} for row in cur.fetchall()}
    except Exception as e:
        print(f"[minutes] ⚠ get_speaker_aliases warning: {e}", flush=True)
        return {}

def apply_speaker_rename(text: str, aliases: dict[str, dict]) -> str:
    """Sostituisce SPEAKER_XX con 'Nome (Azienda)' nel testo."""
    for speaker_id, info in aliases.items():
        label = info["name"]
        if info.get("company"):
            label += f" ({info['company']})"
        text = text.replace(speaker_id, label)
    return text

def get_customer_suggestions(customer: str) -> list[dict]:
    """Suggerisce nomi usati in job precedenti per lo stesso cliente."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT speaker_id, name, company, MAX(created_at) AS last_used
                   FROM speaker_aliases WHERE customer = %s
                   GROUP BY speaker_id, name, company
                   ORDER BY last_used DESC LIMIT 20""",
                (customer,)
            )
            return [{"speaker_id": r[0], "name": r[1], "company": r[2] or "", "last_used": r[3].isoformat() if r[3] else ""} for r in cur.fetchall()]
    except Exception as e:
        print(f"[minutes] ⚠ get_customer_suggestions warning: {e}", flush=True)
        return []

def chunk_text(text: str, chunk_size: int = 300, overlap: int = 50) -> list[str]:
    """Divide il testo in chunk di ~chunk_size parole con overlap."""
    words = text.split()
    chunks = []
    step = chunk_size - overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks

def load_prompt_template(template_name: str) -> str:
    path = PROMPTS_DIR / f"{template_name}.txt"
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Template '{template_name}' non trovato")
    return path.read_text(encoding="utf-8")

def render_prompt(template: str, **kwargs) -> str:
    """Sostituisce {{ var }} e gestisce {% if %} semplici nel template."""
    # Gestisci blocchi {% if var %} ... {% endif %}
    def replace_if(m):
        var_name = m.group(1).strip()
        content = m.group(2)
        return content if kwargs.get(var_name) else ""
    result = re.sub(r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}", replace_if, template, flags=re.DOTALL)
    # Sostituisce {{ var }}
    for key, value in kwargs.items():
        result = result.replace("{{ " + key + " }}", str(value or ""))
        result = result.replace("{{" + key + "}}", str(value or ""))
    return result

def format_duration(segments: list[dict]) -> str:
    if not segments:
        return "N/D"
    total_sec = max(s.get("end", 0) for s in segments)
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    if h:
        return f"{h}h {m:02d}min"
    return f"{m}min"

def build_annotated_transcript(segments: list[dict], aliases: dict[str, dict]) -> str:
    """Costruisce trascrizione annotata con speaker labels rinominati."""
    lines = []
    current_speaker = None
    for seg in segments:
        spk = seg.get("speaker", "UNKNOWN")
        alias = aliases.get(spk)
        if alias:
            label = alias["name"]
            if alias.get("company"):
                label += f" ({alias['company']})"
        else:
            label = spk
        start = seg.get("start", 0)
        mm = int(start // 60)
        ss = int(start % 60)
        text = seg.get("text", "").strip()
        if not text:
            continue
        if spk != current_speaker:
            lines.append(f"\n[{mm:02d}:{ss:02d}] **{label}**")
            current_speaker = spk
        lines.append(text)
    return "\n".join(lines).strip()


# ── RAG ───────────────────────────────────────────────────────────────────────

def rag_index(minutes_text: str, job_id: str, minutes_job_id: str,
              customer: str, project: str, date: str):
    """Indicizza la minuta su Qdrant."""
    embedder = get_embedder()
    client = qdrant()
    if not embedder or not client:
        print("[minutes] ⚠ RAG indexing saltato (embedder o qdrant non disponibili)", flush=True)
        return 0
    try:
        chunks = chunk_text(minutes_text)
        points = []
        for i, chunk in enumerate(chunks):
            vec = embedder.encode(chunk).tolist()
            points.append(PointStruct(
                id=uuid.uuid4().int >> 64,  # uint64
                vector=vec,
                payload={
                    "job_id": job_id,
                    "minutes_job_id": minutes_job_id,
                    "customer": customer,
                    "project": project,
                    "date": date,
                    "chunk_index": i,
                    "text": chunk,
                }
            ))
        client.upsert(collection_name=QDRANT_COLLECTION, points=points)
        print(f"[minutes] ✓ RAG: indicizzati {len(points)} chunk per job {job_id}", flush=True)
        return len(points)
    except Exception as e:
        print(f"[minutes] ⚠ RAG indexing error: {e}", flush=True)
        return 0

def rag_query(query_text: str, customer: str, project: Optional[str] = None, limit: int = 5) -> list[dict]:
    """Recupera chunk rilevanti da Qdrant."""
    embedder = get_embedder()
    client = qdrant()
    if not embedder or not client:
        return []
    try:
        vec = embedder.encode(query_text).tolist()
        # Filtra per customer (obbligatorio), project (opzionale)
        conditions = [FieldCondition(key="customer", match=MatchValue(value=customer))]
        if project:
            conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))
        results = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vec,
            query_filter=Filter(must=conditions),
            limit=limit,
            with_payload=True,
        )
        return [{"text": r.payload.get("text",""), "score": r.score, "date": r.payload.get("date",""), "project": r.payload.get("project","")} for r in results]
    except Exception as e:
        print(f"[minutes] ⚠ RAG query error: {e}", flush=True)
        return []


# ── Ollama ────────────────────────────────────────────────────────────────────

async def ollama_generate(model: str, prompt: str) -> str:
    """Chiama Ollama /api/generate e restituisce il testo completo."""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
        )
        resp.raise_for_status()
        return resp.json().get("response", "")


# ── Minutes generation worker ─────────────────────────────────────────────────

def _run_minutes_job(minutes_job_id: str):
    """Eseguito in thread separato. Genera la minuta e salva risultati."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, template, model, use_rag FROM minutes_jobs WHERE minutes_job_id = %s",
                (minutes_job_id,)
            )
            row = cur.fetchone()
            if not row:
                return
            job_id, template_name, model, use_rag = row

        # Segna WORKING
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE minutes_jobs SET status='WORKING', started_at=now() WHERE minutes_job_id=%s",
                (minutes_job_id,)
            )

        # Dati job
        job_meta = get_job_from_redis(job_id)
        customer  = job_meta.get("customer", "")
        project   = job_meta.get("project", "")
        date_str  = (job_meta.get("created_at") or utc_now())[:10]

        # Trascrizione
        full_text, segments = get_transcript_text(job_id)
        aliases = get_speaker_aliases(job_id)
        annotated = build_annotated_transcript(segments, aliases)
        duration = format_duration(segments)

        # Partecipanti
        participants_list = [f"{v['name']}{' (' + v['company'] + ')' if v.get('company') else ''}" for v in aliases.values()]
        participants_str = ", ".join(participants_list) if participants_list else job_meta.get("participants", "Non specificati")

        # RAG
        rag_context = ""
        rag_chunks_used = 0
        rag_query_text = ""
        if use_rag:
            rag_query_text = full_text[:800]
            chunks = rag_query(rag_query_text, customer, project, limit=5)
            if chunks:
                rag_chunks_used = len(chunks)
                rag_context = "\n\n---\n\n".join(
                    f"[{c.get('date','')} · {c.get('project','')}]\n{c['text']}" for c in chunks
                )

        # Render prompt
        template_str = load_prompt_template(template_name)
        prompt = render_prompt(
            template_str,
            customer=customer,
            project=project,
            date=date_str,
            participants=participants_str,
            duration=duration,
            transcript=annotated or full_text,
            rag_context=rag_context,
            rag_chunks_used=str(rag_chunks_used),
        )

        # Genera con Ollama
        minutes_text = asyncio.run(ollama_generate(model, prompt))

        # Salva su MinIO
        date_str_file = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe_name = f"{customer} - {project} - {date_str_file}.md"
        minio_key = f"{customer}/{project}/{date_str_file}_{job_id[:8]}/{safe_name}"
        client_s3 = s3()
        try:
            client_s3.head_bucket(Bucket=MINIO_BUCKET)
        except ClientError:
            client_s3.create_bucket(Bucket=MINIO_BUCKET)
        client_s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=minio_key,
            Body=minutes_text.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        output_url = f"{MINIO_PUBLIC_URL}/{MINIO_BUCKET}/{minio_key}"

        # Indicizza su Qdrant
        indexed = rag_index(minutes_text, job_id, minutes_job_id, customer, project, date_str)

        # Aggiorna PostgreSQL
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE minutes_jobs
                   SET status='COMPLETED', finished_at=now(),
                       output_url=%s, rag_chunks_used=%s,
                       rag_query=%s, qdrant_indexed=%s
                   WHERE minutes_job_id=%s""",
                (output_url, rag_chunks_used, rag_query_text[:500], indexed > 0, minutes_job_id)
            )
            cur.execute(
                "UPDATE wx_transcription_jobs SET output_minutes_url=%s WHERE job_id=%s",
                (output_url, job_id)
            )

        # Scrivi minutes_url anche su Redis wx:job → wx_api.py lo espone nel GET /jobs/{id}
        wx_key = f"wx:job:{job_id}"
        if r.exists(wx_key):
            r.hset(wx_key, "minutes_url", output_url)

        print(f"[minutes] ✓ minuta {minutes_job_id} COMPLETATA → {output_url}", flush=True)

    except Exception as e:
        print(f"[minutes] ✗ errore minuta {minutes_job_id}: {e}", flush=True)
        try:
            with pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE minutes_jobs SET status='FAILED', error_message=%s WHERE minutes_job_id=%s",
                    (str(e)[:1000], minutes_job_id)
                )
        except Exception:
            pass


# ── Pydantic Models ───────────────────────────────────────────────────────────

class SpeakerAlias(BaseModel):
    speaker_id: str
    name: str
    company: Optional[str] = ""

class SpeakerMappingRequest(BaseModel):
    aliases: list[SpeakerAlias]

class GenerateRequest(BaseModel):
    job_id: str
    template: str = "standard"
    model: str
    use_rag: bool = True


# ── Routes ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    ensure_qdrant_collection()
    print("[minutes] API pronta.", flush=True)


@app.get("/health")
async def health():
    # Verifica Ollama
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            ollama_ok = resp.status_code == 200
    except Exception:
        pass
    # Verifica Qdrant
    qdrant_ok = False
    try:
        qc = qdrant()
        if qc:
            qc.get_collections()
            qdrant_ok = True
    except Exception:
        pass
    return {
        "ok": True,
        "ollama": ollama_ok,
        "qdrant": qdrant_ok,
        "qdrant_collection": QDRANT_COLLECTION,
        "embed_model": EMBED_MODEL,
    }


@app.get("/jobs/{job_id}/transcript-url")
async def get_transcript_url(job_id: str):
    """
    Restituisce l'URL pubblico del result.json su MinIO per questo job.
    Il browser può fetcharlo direttamente (bucket pubblico in download).
    """
    job_meta = get_job_from_redis(job_id)
    url = job_meta.get("output_json_url", "")
    if not url:
        # fallback: prova da PostgreSQL
        try:
            with pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT output_json_url FROM wx_transcription_jobs WHERE job_id = %s",
                    (job_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    url = row[0]
        except Exception:
            pass
    if not url:
        raise HTTPException(status_code=404, detail="output_json_url non trovato per questo job")
    return {"job_id": job_id, "url": url}


@app.get("/jobs/{job_id}/speakers")
async def get_speakers(job_id: str):
    """
    Restituisce:
    - speaker rilevati nel job (da result.json su MinIO)
    - mapping già salvato per questo job (se esiste)
    - suggerimenti da job precedenti dello stesso cliente
    """
    job_meta = get_job_from_redis(job_id)
    customer = job_meta.get("customer", "")
    speakers = get_job_speakers_from_redis(job_id)
    existing = get_speaker_aliases(job_id)
    suggestions = get_customer_suggestions(customer)
    _, segments = get_transcript_text(job_id)

    # Statistiche per speaker: N segmenti, durata totale
    stats: dict[str, dict] = {}
    for seg in segments:
        spk = seg.get("speaker", "UNKNOWN_x")
        if spk not in stats:
            stats[spk] = {"segments": 0, "duration_sec": 0.0}
        stats[spk]["segments"] += 1
        stats[spk]["duration_sec"] += seg.get("end", 0) - seg.get("start", 0)

    return {
        "job_id": job_id,
        "customer": customer,
        "project": job_meta.get("project", ""),
        "speakers": [
            {
                "speaker_id": spk,
                "segments": stats.get(spk, {}).get("segments", 0),
                "duration_sec": round(stats.get(spk, {}).get("duration_sec", 0), 1),
                "mapped": spk in existing,
                "name": existing.get(spk, {}).get("name", ""),
                "company": existing.get(spk, {}).get("company", ""),
            }
            for spk in speakers
        ],
        "suggestions": suggestions,
        "speakers_mapped": job_meta.get("speakers_mapped") == "true",
    }


@app.post("/jobs/{job_id}/speakers")
async def save_speakers(job_id: str, body: SpeakerMappingRequest):
    """Salva il mapping speaker → nome/azienda per il job."""
    job_meta = get_job_from_redis(job_id)
    customer = job_meta.get("customer", "")
    try:
        with pg() as conn, conn.cursor() as cur:
            for alias in body.aliases:
                cur.execute(
                    """INSERT INTO speaker_aliases (job_id, speaker_id, name, company, customer)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, speaker_id) DO UPDATE
                         SET name=EXCLUDED.name, company=EXCLUDED.company, updated_at=now()""",
                    (job_id, alias.speaker_id, alias.name, alias.company or "", customer)
                )
            cur.execute(
                "UPDATE wx_transcription_jobs SET speakers_mapped=true WHERE job_id=%s",
                (job_id,)
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore salvataggio: {e}")

    # Aggiorna Redis
    r.hset(job_key(job_id), "speakers_mapped", "true")

    return {"ok": True, "job_id": job_id, "aliases_saved": len(body.aliases)}


@app.get("/ollama/models")
async def ollama_models():
    """Restituisce i modelli installati su Ollama."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [
                {
                    "name": m["name"],
                    "size_gb": round(m.get("size", 0) / 1e9, 1),
                    "modified": m.get("modified_at", ""),
                }
                for m in data.get("models", [])
            ]
            return {"models": models, "count": len(models)}
    except Exception as e:
        return {"models": [], "count": 0, "error": str(e)}


@app.post("/minutes/generate", status_code=202)
async def generate_minutes(body: GenerateRequest):
    """Avvia la generazione della minuta in background."""
    # Verifica job esiste e trascrizione disponibile
    job_meta = get_job_from_redis(body.job_id)
    if job_meta.get("status") != "COMPLETED":
        raise HTTPException(status_code=400, detail="Il job di trascrizione non è ancora COMPLETED")

    minutes_job_id = uuid.uuid4().hex

    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO minutes_jobs
                   (minutes_job_id, job_id, template, model, use_rag, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, 'PENDING', now(), now())""",
                (minutes_job_id, body.job_id, body.template, body.model, body.use_rag)
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore creazione job: {e}")

    # Avvia in thread (non blocca la response)
    t = threading.Thread(target=_run_minutes_job, args=(minutes_job_id,), daemon=True)
    t.start()

    return {
        "minutes_job_id": minutes_job_id,
        "job_id": body.job_id,
        "status": "PENDING",
        "model": body.model,
        "use_rag": body.use_rag,
    }


@app.get("/minutes/{minutes_job_id}")
def get_minutes_job(minutes_job_id: str):
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT minutes_job_id, job_id, status, template, model,
                          use_rag, rag_chunks_used, output_url, error_message,
                          created_at, updated_at, started_at, finished_at
                   FROM minutes_jobs WHERE minutes_job_id=%s""",
                (minutes_job_id,)
            )
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not row:
        raise HTTPException(status_code=404, detail="minutes job non trovato")
    cols = ["minutes_job_id","job_id","status","template","model","use_rag",
            "rag_chunks_used","output_url","error_message",
            "created_at","updated_at","started_at","finished_at"]
    return {c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)}


@app.get("/minutes/by-job/{job_id}")
def get_minutes_by_job(job_id: str):
    """Restituisce l'ultima minuta generata per un job di trascrizione."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT minutes_job_id, job_id, status, template, model,
                          use_rag, rag_chunks_used, output_url, error_message,
                          created_at, updated_at, started_at, finished_at
                   FROM minutes_jobs WHERE job_id=%s
                   ORDER BY created_at DESC LIMIT 1""",
                (job_id,)
            )
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not row:
        return {"minutes_job_id": None, "job_id": job_id, "status": "NONE"}
    cols = ["minutes_job_id","job_id","status","template","model","use_rag",
            "rag_chunks_used","output_url","error_message",
            "created_at","updated_at","started_at","finished_at"]
    return {c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)}


@app.get("/rag/search")
async def rag_search(
    q: str = Query(..., description="Testo da cercare"),
    customer: str = Query(..., description="Filtra per cliente"),
    project: Optional[str] = Query(None, description="Filtra per progetto (opzionale)"),
    limit: int = Query(5, description="Numero risultati"),
):
    """Debug endpoint: cerca nella RAG."""
    results = rag_query(q, customer, project, limit)
    return {"query": q, "customer": customer, "project": project, "results": results}