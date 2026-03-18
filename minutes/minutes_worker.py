"""
minutes_worker.py — Meeting Minutes Generation Worker
Pipeline:
  1. Fetch trascrizione da MinIO + speaker mapping da Redis
  2. Retrieval contestuale da Qdrant (se context_level != standalone)
  3. Build prompt con template selezionato
  4. Generazione via Ollama (streaming → accumula)
  5. Salva minuta su MinIO (Markdown) + aggiorna PostgreSQL

Coda Redis: minutes:queue
"""

import os
import json
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import redis
import boto3
import psycopg2
import requests
from botocore.exceptions import ClientError
from urllib.parse import urlparse

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL      = os.getenv("REDIS_URL",            "redis://redis:6379/0")
QUEUE_KEY      = os.getenv("MINUTES_QUEUE_KEY",    "minutes:queue")
BLPOP_TIMEOUT  = int(os.getenv("MINUTES_BLPOP_TIMEOUT", "5"))

MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "http://minio:9000")   # boto3 — interno Docker
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", "http://localhost:9000") # URL salvato in DB/Redis
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY",   "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY",   "minioadmin")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET",         "wx-transcriptions")
MINUTES_BUCKET = os.getenv("MINUTES_BUCKET",       "minutes")
MINIO_SECURE   = os.getenv("MINIO_SECURE",         "false").lower() == "true"

QDRANT_URL        = os.getenv("QDRANT_URL",        "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "transcriptions")
EMBED_MODEL       = os.getenv("EMBED_MODEL",       "nomic-embed-text")

OLLAMA_URL     = os.getenv("OLLAMA_URL",           "http://ollama:11434")

# Numero di chunks Qdrant da recuperare per il contesto
RAG_TOP_K      = int(os.getenv("RAG_TOP_K", "8"))

PG_DSN = (
    f"host={os.getenv('PGHOST','postgres')} "
    f"port={os.getenv('PGPORT','5432')} "
    f"dbname={os.getenv('PGDATABASE','n8n')} "
    f"user={os.getenv('PGUSER','n8n')} "
    f"password={os.getenv('PGPASSWORD','')}"
)

# ── Templates ─────────────────────────────────────────────────────────────────
# I template sono file .txt in TEMPLATES_DIR (montato da ./minutes/templates/).
# Ogni file ha un header di metadati seguito da "---" e poi il prompt:
#
#   Title: Meeting Cliente
#   Type: client_meeting
#   Description: Formato PM senior per riunioni enterprise
#   ---
#   <prompt con variabili {{ customer }}, {{ transcript }}, ecc.>
#
# Aggiungere un template = creare un file .txt nella cartella. Zero codice.

TEMPLATES_DIR = Path(os.getenv("MINUTES_TEMPLATES_DIR", "/app/templates"))

def _parse_template_file(path: Path) -> dict:
    """
    Legge un file template e restituisce:
      { key, name, type, description, prompt_text }
    L'header termina alla prima riga "---".
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    meta = {}
    prompt_lines = []
    in_header = True

    for line in lines:
        if in_header:
            if line.strip() == "---":
                in_header = False
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
        else:
            prompt_lines.append(line)

    tags_raw = meta.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    return {
        "key":         path.stem,
        "name":        meta.get("title",       path.stem),
        "tags":        tags,
        "description": meta.get("description", ""),
        "prompt_text": "\n".join(prompt_lines).strip(),
    }

FALLBACK_PROMPT = """Sei un assistente specializzato nella redazione di verbali e minute di riunione in italiano.

Cliente: {{ customer }}
Progetto: {{ project }}
Data: {{ date }}
Partecipanti: {{ participants }}

{% if rag_context %}
## Contesto da riunioni precedenti
{{ rag_context }}
---
{% endif %}

## Trascrizione
{{ transcript }}

---

Produci ESCLUSIVAMENTE il markdown della minuta con: Oggetto, Punti Discussi, Decisioni Prese, Azioni e Responsabilità (tabella), Prossimi Passi.
"""

FALLBACK_TEMPLATES = {
    "standard": {
        "key": "standard", "name": "Verbale Standard",
        "tags": ["verbale", "formale", "decisioni", "azioni"],
        "description": "Verbale formale con sezioni fisse: oggetto, punti discussi, decisioni, azioni, prossimi passi",
        "prompt_text": FALLBACK_PROMPT,
    },
    "client_meeting": {
        "key": "client_meeting", "name": "Meeting Cliente",
        "tags": ["cliente", "enterprise", "PM", "rischi", "tecnico"],
        "description": "Formato PM senior per riunioni enterprise: tipo meeting, decisioni, rischi, note tecniche",
        "prompt_text": FALLBACK_PROMPT,
    },
}

def discover_templates() -> dict[str, dict]:
    """
    Scansiona TEMPLATES_DIR e restituisce tutti i template disponibili.
    Se la cartella non esiste o è vuota, usa FALLBACK_TEMPLATES.
    """
    if not TEMPLATES_DIR.exists():
        print(f"[worker] ⚠ TEMPLATES_DIR non trovata: {TEMPLATES_DIR} — uso fallback", flush=True)
        return dict(FALLBACK_TEMPLATES)
    result = {}
    for path in sorted(TEMPLATES_DIR.glob("*.txt")):
        try:
            tpl = _parse_template_file(path)
            result[tpl["key"]] = tpl
            print(f"[worker] template caricato: {tpl['key']} → {tpl['name']}", flush=True)
        except Exception as e:
            print(f"[worker] ⚠ errore parsing template {path.name}: {e}", flush=True)
    if not result:
        print(f"[worker] ⚠ nessun .txt in {TEMPLATES_DIR} — uso fallback", flush=True)
        return dict(FALLBACK_TEMPLATES)
    return result

# Cache in memoria — ricaricata se vuota o su errore
_templates_cache: dict[str, dict] = {}

def get_templates() -> dict[str, dict]:
    global _templates_cache
    if not _templates_cache:
        _templates_cache = discover_templates()
    return _templates_cache

def get_template(key: str) -> dict:
    """Restituisce il template per chiave; fallback al primo disponibile."""
    templates = get_templates()
    if key in templates:
        return templates[key]
    fallback = next(iter(templates.values()), None)
    if fallback:
        print(f"[worker] ⚠ template '{key}' non trovato, uso '{fallback['key']}'", flush=True)
        return fallback
    raise ValueError(f"Nessun template disponibile in {TEMPLATES_DIR}")

def render_template(template_text: str, variables: dict) -> str:
    """
    Sostituzione variabili {{ var }} e blocchi {% if var %} ... {% endif %}.
    Implementazione minimale senza dipendenze esterne.
    """
    import re

    def replace_if(m):
        var_name = m.group(1).strip()
        inner    = m.group(2)
        return inner if variables.get(var_name) else ""

    result = re.sub(
        r'\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}',
        replace_if, template_text, flags=re.DOTALL,
    )

    def replace_var(m):
        return str(variables.get(m.group(1).strip(), ""))

    return re.sub(r'\{\{\s*(\w+)\s*\}\}', replace_var, result)

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


def ensure_bucket(s3, bucket: str):
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket", "NotFound"):
            s3.create_bucket(Bucket=bucket)
        else:
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def minutes_key(minutes_id: str) -> str:
    return f"minutes:job:{minutes_id}"


def update_minutes_redis(minutes_id: str, **fields):
    fields["updated_at"] = utc_now()
    r.hset(minutes_key(minutes_id), mapping={k: str(v) for k, v in fields.items()})


def pg_update_minutes(minutes_id: str, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [minutes_id]
    sql = f"""
        UPDATE minutes_jobs
        SET {set_clause}, updated_at = now()
        WHERE minutes_id = %s
    """
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, values)
    except Exception as e:
        print(f"[minutes_worker] PG update warning: {e}", flush=True)


# ── Fetch trascrizione ────────────────────────────────────────────────────────

def _parse_minio_url(url: str) -> tuple[str, str]:
    """
    Estrae (bucket, key) da un URL MinIO pubblico o interno.
    Funziona indipendentemente da localhost:9000 vs minio:9000.
    """
    parsed = urlparse(url)
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if len(path_parts) != 2 or not path_parts[0] or not path_parts[1]:
        raise ValueError(f"URL MinIO non valido (atteso /bucket/key): {url}")
    return path_parts[0], path_parts[1]


def fetch_transcript(job_id: str) -> Optional[dict]:
    s3 = s3_client()
    json_url = r.hget(f"wx:job:{job_id}", "output_json_url") or ""

    if json_url.startswith("http"):
        try:
            bucket, key = _parse_minio_url(json_url)
            obj = s3.get_object(Bucket=bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception as e:
            print(f"[minutes_worker] fallback scan: {e}", flush=True)

    # Fallback scan
    customer = r.hget(f"wx:job:{job_id}", "customer") or ""
    project  = r.hget(f"wx:job:{job_id}", "project")  or ""
    prefix   = f"{customer}/{project}/" if customer and project else ""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if job_id[:8] in key and key.endswith("result.json"):
                data = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
                return json.loads(data["Body"].read())
    return None


def get_speaker_aliases_as_participants(job_id: str) -> list[dict]:
    """
    Legge speaker_aliases da PG e li converte nel formato participants
    [{speaker, name, role}] usato dal worker.
    Sorgente di verità: sempre prioritaria su quanto passato dalla UI.
    """
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT speaker_id, name, company FROM speaker_aliases "
                "WHERE job_id = %s ORDER BY speaker_id",
                (job_id,)
            )
            rows = cur.fetchall()
        if rows:
            print(f"[minutes_worker] speaker_aliases da PG: {len(rows)} speaker per job {job_id[:8]}", flush=True)
        return [
            {"speaker": row[0], "name": row[1], "role": row[2] or ""}
            for row in rows
        ]
    except Exception as e:
        print(f"[minutes_worker] ⚠ get_speaker_aliases warning: {e}", flush=True)
        return []


# ── Speaker mapping ───────────────────────────────────────────────────────────

def apply_speaker_mapping(segments: list[dict], speaker_map: dict) -> list[dict]:
    """Sostituisce SPEAKER_00 → nome reale se presente nel mapping."""
    if not speaker_map:
        return segments
    return [
        {**seg, "speaker": speaker_map.get(seg.get("speaker", ""), seg.get("speaker", "UNKNOWN"))}
        for seg in segments
    ]


def format_transcript_for_prompt(segments: list[dict], max_chars: int = 12000) -> str:
    """
    Formatta la trascrizione per il prompt LLM.
    Raggruppa per speaker e tronca se troppo lunga.
    """
    lines = []
    current_speaker = None
    current_texts   = []

    for seg in segments:
        spk  = seg.get("speaker", "UNKNOWN")
        text = seg.get("text", "").strip()
        if not text:
            continue
        if spk != current_speaker:
            if current_speaker and current_texts:
                lines.append(f"{current_speaker}: {' '.join(current_texts)}")
            current_speaker = spk
            current_texts   = [text]
        else:
            current_texts.append(text)

    if current_speaker and current_texts:
        lines.append(f"{current_speaker}: {' '.join(current_texts)}")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[... trascrizione troncata per lunghezza ...]"
    return result


# ── RAG Retrieval ─────────────────────────────────────────────────────────────

def embed_text(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def build_qdrant_filter(context_level: str, customer: str,
                         project: str, speakers: list[str]) -> Optional[dict]:
    """Costruisce il filtro Qdrant in base al context_level scelto dall'utente."""
    if context_level == "standalone":
        return None

    must = []

    if context_level == "customer":
        must.append({"key": "customer", "match": {"value": customer}})

    elif context_level == "customer_project":
        must.append({"key": "customer", "match": {"value": customer}})
        must.append({"key": "project",  "match": {"value": project}})

    elif context_level == "speakers" and speakers:
        # Cerca chunks in cui almeno uno degli speaker mappati è presente
        must.append({
            "key": "speaker",
            "match": {"any": speakers},
        })

    return {"must": must} if must else None


def retrieve_context(query: str, context_level: str, customer: str,
                     project: str, speakers: list[str],
                     exclude_job_id: str) -> str:
    """
    Retrieval da Qdrant.
    Esclude il job corrente (già incluso come trascrizione principale).
    Restituisce testo contestuale formattato.
    """
    if context_level == "standalone":
        return ""

    try:
        vector = embed_text(query)
    except Exception as e:
        print(f"[minutes_worker] ⚠ embed fallito per retrieval: {e}", flush=True)
        return ""

    qdrant_filter = build_qdrant_filter(context_level, customer, project, speakers)

    # Aggiungi esclusione job corrente
    must_not = [{"key": "job_id", "match": {"value": exclude_job_id}}]

    search_body = {
        "vector":      vector,
        "limit":       RAG_TOP_K,
        "with_payload": True,
        "filter": {
            "must":     qdrant_filter.get("must", []) if qdrant_filter else [],
            "must_not": must_not,
        },
    }

    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
            json=search_body,
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
    except Exception as e:
        print(f"[minutes_worker] ⚠ retrieval fallito: {e}", flush=True)
        return ""

    if not results:
        return ""

    # Formatta il contesto recuperato
    context_lines = []
    for hit in results:
        payload = hit.get("payload", {})
        date    = payload.get("date", "")
        project_ctx = payload.get("project", "")
        speaker = payload.get("speaker", "")
        text    = payload.get("text", "")
        score   = round(hit.get("score", 0), 3)
        context_lines.append(
            f"[{date} | {project_ctx} | {speaker} | score:{score}]\n{text}"
        )

    return "\n\n---\n\n".join(context_lines)


# ── Prompt Builder ────────────────────────────────────────────────────────────


# ── Prompt Builder ────────────────────────────────────────────────────────────

def build_prompt(
    transcript_text: str,
    context_text: str,
    template_key: str,
    language: str,
    customer: str,
    project: str,
    participants: list[dict],
    meeting_date: str,
    context_level: str,
) -> tuple[str, str]:
    """
    Carica il file template, sostituisce le variabili e restituisce
    (system_prompt, user_prompt) per Ollama.

    I template sono file .txt in TEMPLATES_DIR con variabili {{ var }}.
    Il testo del template diventa il user_prompt.
    Il system_prompt è fisso e breve (istruzione lingua + "no markdown extra").
    """
    # Participants: costruisci stringa leggibile
    if participants:
        participants_str = ", ".join(
            f"{p.get('name', p.get('speaker', '?'))}"
            + (f" ({p.get('role', '')})" if p.get("role") else "")
            for p in participants
        )
    else:
        participants_str = "da inferire dalla trascrizione"

    # Calcola durata approssimativa dalla trascrizione (conta le righe di testo)
    lines = [l for l in transcript_text.splitlines() if l.strip()]
    duration_str = f"~{max(1, len(lines) // 10)} minuti stimati" if lines else "non disponibile"

    variables = {
        "customer":        customer,
        "project":         project,
        "date":            meeting_date,
        "participants":    participants_str,
        "duration":        duration_str,
        "transcript":      transcript_text,
        "rag_context":     context_text.strip(),
        "rag_chunks_used": str(context_text.count("---")) if context_text.strip() else "0",
    }

    template_text = get_template(template_key)["prompt_text"]
    user_prompt   = render_template(template_text, variables)

    lang_instruction = (
        "Rispondi SEMPRE in italiano, indipendentemente dalla lingua della trascrizione."
        if language == "it"
        else f"Rispondi nella lingua della trascrizione ({language})."
    )

    system_prompt = (
        f"{lang_instruction} "
        "Produci ESCLUSIVAMENTE il testo della minuta in Markdown. "
        "Non aggiungere commenti, prefazioni o spiegazioni al di fuori della minuta stessa."
    )

    return system_prompt, user_prompt


# ── Ollama Generation ─────────────────────────────────────────────────────────

def generate_minutes(system_prompt: str, user_prompt: str,
                     model: str, minutes_id: str) -> str:
    """
    Chiama Ollama in modalità streaming.
    Aggiorna Redis con progresso ogni ~500 token.
    """
    url = f"{OLLAMA_URL}/api/chat"
    payload = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "options": {
            "temperature": 0.3,   # bassa per output deterministico e fattuale
            "num_predict": 4096,
        },
    }

    full_text    = []
    token_count  = 0
    last_update  = 0

    with requests.post(url, json=payload, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            content = chunk.get("message", {}).get("content", "")
            if content:
                full_text.append(content)
                token_count += 1
                # Aggiorna Redis ogni 100 token (throttle)
                if token_count - last_update >= 100:
                    update_minutes_redis(
                        minutes_id,
                        step=f"GENERATING_{token_count}tok",
                    )
                    last_update = token_count

            if chunk.get("done"):
                break

    return "".join(full_text)


# ── Upload minuta su MinIO ────────────────────────────────────────────────────

def upload_minutes(minutes_id: str, job_id: str, customer: str,
                    project: str, content: str) -> str:
    s3 = s3_client()
    ensure_bucket(s3, MINUTES_BUCKET)

    date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_name = f"{customer} - {project} - {date_str}.md"
    key       = f"{customer}/{project}/{date_str}_{job_id[:8]}/{safe_name}"

    s3.put_object(
        Bucket=MINUTES_BUCKET,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )

    url = f"{MINIO_PUBLIC_URL}/{MINUTES_BUCKET}/{key}"
    print(f"[minutes_worker] upload OK: {url}", flush=True)
    return url


# ── Core job ──────────────────────────────────────────────────────────────────

def run_job(minutes_id: str):
    k = minutes_key(minutes_id)

    job_id        = r.hget(k, "job_id")        or ""
    template_key  = r.hget(k, "template")      or "operativa"
    context_level = r.hget(k, "context_level") or "standalone"
    model         = r.hget(k, "model")         or "mistral:7b"
    language      = r.hget(k, "language")      or "it"
    customer      = r.hget(k, "customer")      or ""
    project       = r.hget(k, "project")       or ""
    participants_raw = r.hget(k, "participants") or "[]"

    try:
        participants = json.loads(participants_raw)
    except Exception:
        participants = []

    # ── Sorgente di verità: speaker_aliases su PG ────────────────────────────
    # I participants passati dalla UI possono essere vuoti o stale (bug porto 9200).
    # Leggiamo sempre da speaker_aliases che è scritto da transcriber_api al salvataggio.
    db_participants = get_speaker_aliases_as_participants(job_id)
    if db_participants:
        participants = db_participants
        print(f"[minutes_worker] uso speaker_aliases da PG ({len(participants)} speaker)", flush=True)
    elif participants:
        print(f"[minutes_worker] uso participants da Redis ({len(participants)} speaker)", flush=True)
    else:
        print(f"[minutes_worker] ⚠ nessun speaker mapping disponibile per job {job_id[:8]}", flush=True)

    # Speaker names per filtro RAG
    speaker_names = [
        p.get("name", p.get("speaker", ""))
        for p in participants
        if p.get("name") or p.get("speaker")
    ]

    update_minutes_redis(minutes_id, status="WORKING", step="FETCHING_TRANSCRIPT")
    pg_update_minutes(minutes_id, status="WORKING", started_at=utc_now())

    # ── 1. Fetch trascrizione ────────────────────────────────────────────────
    transcript = fetch_transcript(job_id)
    if not transcript:
        update_minutes_redis(minutes_id, status="FAILED", step="FETCH_FAILED",
                             error="trascrizione non trovata")
        pg_update_minutes(minutes_id, status="FAILED",
                          error_message="trascrizione non trovata")
        return

    segments     = transcript.get("segments", [])
    meeting_date = (transcript.get("created_at") or utc_now())[:10]

    # ── 2. Applica speaker mapping ───────────────────────────────────────────
    speaker_map = {p["speaker"]: p["name"] for p in participants if p.get("speaker") and p.get("name")}
    segments    = apply_speaker_mapping(segments, speaker_map)

    transcript_text = format_transcript_for_prompt(segments)
    print(f"[minutes_worker] trascrizione: {len(transcript_text)} chars", flush=True)

    # ── 3. RAG retrieval ─────────────────────────────────────────────────────
    update_minutes_redis(minutes_id, step="RAG_RETRIEVAL")
    context_text = ""

    if context_level != "standalone":
        print(f"[minutes_worker] retrieval context_level={context_level}", flush=True)
        # Query sintetica per il retrieval
        query = f"riunione {customer} {project} decisioni action items"
        context_text = retrieve_context(
            query, context_level, customer, project,
            speaker_names, exclude_job_id=job_id,
        )
        print(f"[minutes_worker] contesto RAG: {len(context_text)} chars", flush=True)

    # ── 4. Build prompt ──────────────────────────────────────────────────────
    update_minutes_redis(minutes_id, step="BUILDING_PROMPT")
    system_prompt, user_prompt = build_prompt(
        transcript_text=transcript_text,
        context_text=context_text,
        template_key=template_key,
        language=language,
        customer=customer,
        project=project,
        participants=participants,
        meeting_date=meeting_date,
        context_level=context_level,
    )

    print(
        f"[minutes_worker] prompt: system={len(system_prompt)}c "
        f"user={len(user_prompt)}c model={model}",
        flush=True,
    )

    # ── 5. Generazione LLM ───────────────────────────────────────────────────
    update_minutes_redis(minutes_id, step="GENERATING_0tok")
    try:
        minutes_content = generate_minutes(system_prompt, user_prompt, model, minutes_id)
    except Exception as e:
        update_minutes_redis(minutes_id, status="FAILED", step="LLM_FAILED", error=str(e))
        pg_update_minutes(minutes_id, status="FAILED", error_message=f"LLM: {e}")
        return

    if not minutes_content.strip():
        update_minutes_redis(minutes_id, status="FAILED", step="LLM_EMPTY",
                             error="output LLM vuoto")
        pg_update_minutes(minutes_id, status="FAILED",
                          error_message="output LLM vuoto")
        return

    print(f"[minutes_worker] generati {len(minutes_content)} chars", flush=True)

    # ── 6. Upload MinIO ──────────────────────────────────────────────────────
    update_minutes_redis(minutes_id, step="UPLOADING")
    try:
        output_url = upload_minutes(minutes_id, job_id, customer, project, minutes_content)
    except Exception as e:
        update_minutes_redis(minutes_id, status="FAILED", step="UPLOAD_FAILED", error=str(e))
        pg_update_minutes(minutes_id, status="FAILED", error_message=f"upload: {e}")
        return

    # ── 7. Salva anche in Redis per preview rapida (primi 8k chars) ──────────
    r.hset(k, "preview", minutes_content[:8000])

    # ── 8. Completato ────────────────────────────────────────────────────────
    update_minutes_redis(minutes_id, status="COMPLETED", step="DONE", output_url=output_url)
    pg_update_minutes(minutes_id, status="COMPLETED",
                      output_url=output_url,
                      finished_at=utc_now())

    # ── 9. Scrivi minutes_url anche su wx:job:{job_id} ────────────────────────
    # wx_api.py espone questo campo nel GET /jobs/{job_id} → UI può mostrarlo
    wx_key = f"wx:job:{job_id}"
    if r.exists(wx_key):
        r.hset(wx_key, "minutes_url", output_url)
        print(f"[minutes_worker] minutes_url scritto su {wx_key}", flush=True)

    print(f"[minutes_worker] ✓ minuta {minutes_id} COMPLETED → {output_url}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print(
        f"[minutes_worker] Avvio — redis={REDIS_URL} queue={QUEUE_KEY} "
        f"ollama={OLLAMA_URL} qdrant={QDRANT_URL}",
        flush=True,
    )

    # Attendi Ollama
    for attempt in range(30):
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            if resp.ok:
                print("[minutes_worker] Ollama pronto", flush=True)
                break
        except Exception:
            pass
        print(f"[minutes_worker] attesa Ollama ({attempt+1}/30)...", flush=True)
        time.sleep(2)

    print("[minutes_worker] in ascolto su coda...", flush=True)

    while True:
        item = r.blpop(QUEUE_KEY, timeout=BLPOP_TIMEOUT)
        if not item:
            continue
        _, minutes_id = item
        print(f"[minutes_worker] → job {minutes_id}", flush=True)
        try:
            run_job(minutes_id)
        except Exception as e:
            update_minutes_redis(minutes_id, status="FAILED",
                                 step="WORKER_CRASH", error=str(e))
            pg_update_minutes(minutes_id, status="FAILED",
                              error_message=f"crash: {e}")
            print(f"[minutes_worker] ✗ crash {minutes_id}: {e}", flush=True)


if __name__ == "__main__":
    main()