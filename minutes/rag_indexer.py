"""
rag_indexer.py — RAG Indexer Worker
Ascolta la coda Redis `rag:queue`.
Per ogni job_id ricevuto:
  1. Fetch result.json da MinIO
  2. Chunking per segmento (o per blocco speaker)
  3. Embedding via Ollama (nomic-embed-text) o sentence-transformers fallback
  4. Upsert su Qdrant con metadata filtrabili
  5. Aggiorna rag_index_jobs su PostgreSQL

Metadata Qdrant per ogni chunk:
  job_id, customer, project, language, date,
  speaker, segment_start, segment_end, chunk_index

Avvio automatico: wx_worker fa rpush rag:queue {job_id} appena status=COMPLETED.
"""

import os
import json
import uuid
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import redis
import boto3
import psycopg2
import requests
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL         = os.getenv("REDIS_URL",              "redis://redis:6379/0")
RAG_QUEUE_KEY     = os.getenv("RAG_QUEUE_KEY",          "rag:queue")
BLPOP_TIMEOUT     = int(os.getenv("RAG_BLPOP_TIMEOUT",  "5"))

MINIO_ENDPOINT    = os.getenv("MINIO_ENDPOINT",         "http://minio:9000")
MINIO_ACCESS_KEY  = os.getenv("MINIO_ACCESS_KEY",       "minioadmin")
MINIO_SECRET_KEY  = os.getenv("MINIO_SECRET_KEY",       "minioadmin")
MINIO_BUCKET      = os.getenv("MINIO_BUCKET",           "wx-transcriptions")
MINIO_SECURE      = os.getenv("MINIO_SECURE",           "false").lower() == "true"

QDRANT_URL        = os.getenv("QDRANT_URL",             "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION",      "transcriptions")

OLLAMA_URL        = os.getenv("OLLAMA_URL",             "http://ollama:11434")
EMBED_MODEL       = os.getenv("EMBED_MODEL",            "nomic-embed-text")
EMBED_DIM         = int(os.getenv("EMBED_DIM",          "768"))

# Chunking: raggruppa N segmenti consecutivi dello stesso speaker
CHUNK_SIZE_SEGS   = int(os.getenv("RAG_CHUNK_SIZE_SEGS", "5"))
# Overlap: riprendi gli ultimi N segmenti del chunk precedente
CHUNK_OVERLAP     = int(os.getenv("RAG_CHUNK_OVERLAP",   "1"))

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

# ── Clients ───────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        verify=MINIO_SECURE,
    )


def pg_conn():
    return psycopg2.connect(PG_DSN)


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pg_upsert_rag_job(job_id: str, status: str, chunk_count: int = 0,
                       error: str = ""):
    sql = """
        INSERT INTO rag_index_jobs (job_id, status, chunk_count, error_message, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (job_id) DO UPDATE SET
          status        = EXCLUDED.status,
          chunk_count   = EXCLUDED.chunk_count,
          error_message = EXCLUDED.error_message,
          updated_at    = now(),
          finished_at   = CASE WHEN EXCLUDED.status IN ('COMPLETED','FAILED')
                               THEN now() ELSE rag_index_jobs.finished_at END
    """
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (job_id, status, chunk_count, error))
    except Exception as e:
        print(f"[rag] PG upsert warning: {e}", flush=True)


def fetch_transcript_from_minio(job_id: str) -> Optional[dict]:
    """
    Cerca il result.json del job su MinIO.
    Prova prima a leggere l'URL dal Redis hash wx:job:{job_id},
    poi fa fallback su scansione prefisso.
    """
    s3 = s3_client()

    # Prova con URL diretto da Redis
    json_url = r.hget(f"wx:job:{job_id}", "output_json_url") or ""
    if json_url.startswith("http"):
        # Estrai bucket e key dall'URL: http://minio:9000/bucket/key
        parts = json_url.replace(MINIO_ENDPOINT.rstrip("/") + "/", "").split("/", 1)
        if len(parts) == 2:
            bucket, key = parts
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                return json.loads(obj["Body"].read())
            except Exception as e:
                print(f"[rag] fallback scan dopo errore URL diretto: {e}", flush=True)

    # Fallback: scansiona per customer/project
    customer = r.hget(f"wx:job:{job_id}", "customer") or ""
    project  = r.hget(f"wx:job:{job_id}", "project")  or ""
    prefix   = f"{customer}/{project}/" if customer and project else ""

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if job_id[:8] in key and key.endswith("result.json"):
                try:
                    data = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
                    return json.loads(data["Body"].read())
                except Exception:
                    pass
    return None


# ── Chunking ──────────────────────────────────────────────────────────────────

def build_chunks(segments: list[dict], job_meta: dict) -> list[dict]:
    """
    Raggruppa i segmenti in chunks di CHUNK_SIZE_SEGS con overlap.
    Ogni chunk contiene:
      - text: testo concatenato
      - speaker: speaker prevalente nel chunk
      - start / end: timestamp del chunk
      - chunk_index: indice progressivo
      - metadata: tutti i campi filtrabili per Qdrant
    """
    chunks = []
    step   = max(1, CHUNK_SIZE_SEGS - CHUNK_OVERLAP)

    for i in range(0, len(segments), step):
        segs = segments[i: i + CHUNK_SIZE_SEGS]
        if not segs:
            break

        text = " ".join(s.get("text", "").strip() for s in segs if s.get("text"))
        if not text.strip():
            continue

        # Speaker prevalente per numero di segmenti
        speaker_counts: dict[str, int] = {}
        for s in segs:
            spk = s.get("speaker", "UNKNOWN")
            speaker_counts[spk] = speaker_counts.get(spk, 0) + 1
        dominant_speaker = max(speaker_counts, key=speaker_counts.get)

        chunk_id = hashlib.md5(
            f"{job_meta['job_id']}_{i}".encode()
        ).hexdigest()

        chunks.append({
            "id":      chunk_id,
            "text":    text,
            "speaker": dominant_speaker,
            "start":   segs[0].get("start", 0),
            "end":     segs[-1].get("end", 0),
            "chunk_index": len(chunks),
            "metadata": {
                "job_id":    job_meta["job_id"],
                "customer":  job_meta.get("customer", ""),
                "project":   job_meta.get("project", ""),
                "language":  job_meta.get("language", ""),
                "date":      job_meta.get("created_at", "")[:10],
                "speaker":   dominant_speaker,
                "all_speakers": list({s.get("speaker","UNKNOWN") for s in segs}),
                "chunk_index": len(chunks),
            },
        })

    return chunks


# ── Embedding ─────────────────────────────────────────────────────────────────

def ensure_embed_model():
    """Scarica nomic-embed-text se non presente su Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if resp.ok:
            models = [m["name"] for m in resp.json().get("models", [])]
            if not any(EMBED_MODEL in m for m in models):
                print(f"[rag] pull {EMBED_MODEL}...", flush=True)
                requests.post(
                    f"{OLLAMA_URL}/api/pull",
                    json={"name": EMBED_MODEL, "stream": False},
                    timeout=300,
                )
                print(f"[rag] {EMBED_MODEL} pronto", flush=True)
    except Exception as e:
        print(f"[rag] ⚠ impossibile verificare modello embed: {e}", flush=True)


def embed_text(text: str) -> list[float]:
    """Embed via Ollama nomic-embed-text."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


# ── Qdrant ────────────────────────────────────────────────────────────────────

def ensure_qdrant_collection():
    """Crea la collection Qdrant se non esiste."""
    url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}"
    resp = requests.get(url, timeout=5)
    if resp.status_code == 404:
        print(f"[rag] creazione collection {QDRANT_COLLECTION}...", flush=True)
        requests.put(
            url,
            json={
                "vectors": {
                    "size":     EMBED_DIM,
                    "distance": "Cosine",
                }
            },
            timeout=10,
        )
        # Payload indexes per filtri rapidi
        for field in ("customer", "project", "speaker", "job_id", "date"):
            requests.put(
                f"{url}/index",
                json={"field_name": field, "field_schema": "keyword"},
                timeout=5,
            )
        print(f"[rag] collection pronta", flush=True)


def upsert_chunks(chunks: list[dict]) -> int:
    """Upsert batch di chunks su Qdrant. Restituisce numero di chunks inseriti."""
    if not chunks:
        return 0

    points = []
    for chunk in chunks:
        try:
            vector = embed_text(chunk["text"])
        except Exception as e:
            print(f"[rag] ⚠ embed fallito chunk {chunk['chunk_index']}: {e}", flush=True)
            continue

        points.append({
            "id":      chunk["id"],
            "vector":  vector,
            "payload": {
                **chunk["metadata"],
                "text":  chunk["text"],
                "start": chunk["start"],
                "end":   chunk["end"],
            },
        })

    if not points:
        return 0

    # Batch upsert
    resp = requests.put(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
        json={"points": points},
        timeout=120,
    )
    resp.raise_for_status()
    return len(points)


def delete_job_vectors(job_id: str):
    """Rimuove tutti i vettori di un job dalla collection (per retry)."""
    try:
        requests.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/delete",
            json={"filter": {"must": [{"key": "job_id", "match": {"value": job_id}}]}},
            timeout=10,
        )
    except Exception as e:
        print(f"[rag] ⚠ delete vectors warning: {e}", flush=True)


# ── Core job ──────────────────────────────────────────────────────────────────

def run_index_job(job_id: str):
    print(f"[rag] → indicizzazione job {job_id}", flush=True)
    pg_upsert_rag_job(job_id, "WORKING")

    try:
        # 1. Fetch trascrizione da MinIO
        transcript = fetch_transcript_from_minio(job_id)
        if not transcript:
            raise RuntimeError("result.json non trovato su MinIO")

        segments = transcript.get("segments", [])
        if not segments:
            raise RuntimeError("nessun segmento nella trascrizione")

        job_meta = {
            "job_id":     job_id,
            "customer":   transcript.get("customer", ""),
            "project":    transcript.get("project", ""),
            "language":   transcript.get("language", ""),
            "created_at": transcript.get("created_at", utc_now()),
        }

        print(f"[rag] {len(segments)} segmenti trovati", flush=True)

        # 2. Chunking
        chunks = build_chunks(segments, job_meta)
        print(f"[rag] {len(chunks)} chunks generati", flush=True)

        # 3. Rimuovi eventuali vettori precedenti (retry safe)
        delete_job_vectors(job_id)

        # 4. Embed + upsert
        ensure_qdrant_collection()
        inserted = upsert_chunks(chunks)
        print(f"[rag] {inserted} vettori inseriti su Qdrant", flush=True)

        pg_upsert_rag_job(job_id, "COMPLETED", chunk_count=inserted)
        print(f"[rag] ✓ job {job_id} indicizzato", flush=True)

    except Exception as e:
        print(f"[rag] ✗ errore job {job_id}: {e}", flush=True)
        pg_upsert_rag_job(job_id, "FAILED", error=str(e))


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print(
        f"[rag] Avvio — redis={REDIS_URL} queue={RAG_QUEUE_KEY} "
        f"qdrant={QDRANT_URL} embed={EMBED_MODEL}",
        flush=True,
    )

    # Attendi che Qdrant sia pronto
    for attempt in range(30):
        try:
            resp = requests.get(f"{QDRANT_URL}/healthz", timeout=3)
            if resp.ok:
                print("[rag] Qdrant pronto", flush=True)
                break
        except Exception:
            pass
        print(f"[rag] attesa Qdrant ({attempt+1}/30)...", flush=True)
        time.sleep(2)

    ensure_qdrant_collection()
    ensure_embed_model()

    # Re-accoda eventuali job rimasti PENDING/WORKING da crash precedente
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM rag_index_jobs WHERE status IN ('PENDING','WORKING')"
            )
            stale = [row[0] for row in cur.fetchall()]
        for job_id in stale:
            print(f"[rag] recovery stale job {job_id}", flush=True)
            r.rpush(RAG_QUEUE_KEY, job_id)
    except Exception as e:
        print(f"[rag] ⚠ recovery warning: {e}", flush=True)

    print("[rag] in ascolto su coda...", flush=True)

    while True:
        item = r.blpop(RAG_QUEUE_KEY, timeout=BLPOP_TIMEOUT)
        if not item:
            continue
        _, job_id = item
        try:
            run_index_job(job_id)
        except Exception as e:
            print(f"[rag] ✗ crash job {job_id}: {e}", flush=True)
            pg_upsert_rag_job(job_id, "FAILED", error=f"crash: {e}")


if __name__ == "__main__":
    main()
