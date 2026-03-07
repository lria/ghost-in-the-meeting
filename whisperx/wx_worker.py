"""
wx_worker.py — Transcription Worker
Pipeline: ffmpeg → faster-whisper (trascrizione) → pyannote (diarizzazione) → merge → MinIO → PostgreSQL

Dipendenze chiave (senza whisperX):
  faster-whisper  1.0.0   ← trascrizione
  pyannote.audio  3.3.2   ← diarizzazione speaker
  ctranslate2     4.3.1   ← motore inferenza faster-whisper
"""

import os
import json
import warnings
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# Sopprimi warning deprecazione da transformers e onnxruntime
warnings.filterwarnings("ignore", category=FutureWarning, module="transformers")
warnings.filterwarnings("ignore", category=UserWarning, module="transformers")

import redis
import boto3
import psycopg2
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL         = os.getenv("REDIS_URL",            "redis://redis:6379/0")
QUEUE_KEY         = os.getenv("WX_QUEUE_KEY",         "wx:queue")
BLPOP_TIMEOUT     = int(os.getenv("WX_BLPOP_TIMEOUT", "5"))
STALE_WORKING_SEC = int(os.getenv("WX_STALE_SEC",     str(45 * 60)))

DATA_DIR          = Path(os.getenv("WX_DATA_DIR",     "/data"))
IN_DIR            = DATA_DIR / "in"
OUT_DIR           = DATA_DIR / "out"
IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL     = os.getenv("WHISPER_MODEL",        "small")
WHISPER_DEVICE    = os.getenv("WHISPER_DEVICE",       "cpu")
WHISPER_COMPUTE   = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
HF_TOKEN          = os.getenv("HF_TOKEN",             "")

MINIO_ENDPOINT    = os.getenv("MINIO_ENDPOINT",       "http://minio:9000")
MINIO_ACCESS_KEY  = os.getenv("MINIO_ACCESS_KEY",     "minioadmin")
MINIO_SECRET_KEY  = os.getenv("MINIO_SECRET_KEY",     "minioadmin")
MINIO_BUCKET      = os.getenv("MINIO_BUCKET",         "wx-transcriptions")
MINIO_SECURE      = os.getenv("MINIO_SECURE",         "false").lower() == "true"

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

def ensure_bucket(s3, bucket: str):
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket", "NotFound"):
            s3.create_bucket(Bucket=bucket)
        else:
            raise

def pg_conn():
    return psycopg2.connect(PG_DSN)


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def job_key(job_id: str) -> str:
    return f"wx:job:{job_id}"

def update_job(job_id: str, **fields):
    fields["updated_at"] = utc_now()
    r.hset(job_key(job_id), mapping={k: str(v) for k, v in fields.items()})

def pg_update(job_id: str, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [job_id]
    sql = f"UPDATE wx_transcription_jobs SET {set_clause}, updated_at=now() WHERE job_id = %s"
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, values)
    except Exception as e:
        print(f"[worker] PG update warning: {e}", flush=True)

def ffmpeg_to_wav(src: Path, dst: Path):
    """Converte qualsiasi formato audio/video in WAV mono 16kHz."""
    cmd = ["ffmpeg", "-y", "-i", str(src),
           "-ac", "1", "-ar", "16000", "-vn", str(dst)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Trascrizione con faster-whisper ──────────────────────────────────────────

def transcribe(model, wav_path: Path, language: str) -> tuple[list[dict], str]:
    """
    Trascrive con faster-whisper.
    Restituisce (segments, detected_language).
    Ogni segmento: {start, end, text, words: [{start, end, word, probability}]}
    """
    lang_param = None if language == "auto" else language

    segments_gen, info = model.transcribe(
        str(wav_path),
        language=lang_param,
        beam_size=5,
        word_timestamps=True,   # necessario per il merge con diarizzazione
        vad_filter=True,        # filtra i silenzi, riduce allucinazioni
        vad_parameters={"min_silence_duration_ms": 500},
    )

    segments = []
    for seg in segments_gen:
        words = []
        if seg.words:
            words = [
                {"start": w.start, "end": w.end,
                 "word": w.word, "probability": round(w.probability, 3)}
                for w in seg.words
            ]
        segments.append({
            "start": round(seg.start, 3),
            "end":   round(seg.end,   3),
            "text":  seg.text.strip(),
            "words": words,
        })

    return segments, info.language


# ── Merge trascrizione + diarizzazione ────────────────────────────────────────

def merge_transcript_diarization(segments: list[dict], diarization) -> list[dict]:
    """
    Assegna lo speaker label a ogni segmento trascritto.
    Strategia: per ogni segmento prende lo speaker pyannote con cui
    ha la maggiore sovrapposizione temporale.
    Se nessuna sovrapposizione → UNKNOWN.
    """
    merged = []
    for seg in segments:
        seg_start    = seg["start"]
        seg_end      = seg["end"]
        best_speaker = "UNKNOWN"
        best_overlap = 0.0

        for turn, _, speaker in diarization.itertracks(yield_label=True):
            overlap = max(0.0, min(seg_end, turn.end) - max(seg_start, turn.start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker

        merged.append({**seg, "speaker": best_speaker})

    return merged


# ── Stale recovery ────────────────────────────────────────────────────────────

def recover_stale_jobs():
    """Ri-accoda job rimasti PENDING o WORKING (da crash precedente)."""
    for key in r.scan_iter(match="wx:job:*", count=200):
        status     = r.hget(key, "status") or ""
        job_id     = r.hget(key, "job_id") or key.split(":")[-1]
        input_file = r.hget(key, "input_file") or ""

        if not input_file or not Path(input_file).exists():
            continue

        if status == "PENDING":
            r.rpush(QUEUE_KEY, job_id)
            update_job(job_id, step="RECOVERY_REQUEUED_PENDING")

        elif status == "WORKING":
            updated_at = r.hget(key, "updated_at") or ""
            try:
                ts  = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
            except Exception:
                age = STALE_WORKING_SEC + 1

            if age >= STALE_WORKING_SEC:
                r.rpush(QUEUE_KEY, job_id)
                update_job(job_id, status="PENDING", step="RECOVERY_REQUEUED_STALE")


# ── Core job ──────────────────────────────────────────────────────────────────

def run_job(job_id: str, fw_model, diar_pipeline):
    k          = job_key(job_id)
    input_file = r.hget(k, "input_file")
    language      = r.hget(k, "language")      or "it"
    diarize       = r.hget(k, "diarize")       or "true"
    customer      = r.hget(k, "customer")      or "unknown"
    project       = r.hget(k, "project")       or "unknown"
    max_spk       = int(r.hget(k, "max_speakers") or 8)
    output_format = r.hget(k, "output_format") or "both"  # json | txt | both

    if not input_file or not Path(input_file).exists():
        update_job(job_id, status="FAILED", step="INPUT_CHECK", error="input_missing")
        pg_update(job_id, status="FAILED", error_message="input file not found")
        return

    job_out_dir = OUT_DIR / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Avvio ────────────────────────────────────────────────────────────
    update_job(job_id, status="WORKING", step="FFMPEG_START")
    pg_update(job_id, status="WORKING", started_at=utc_now())

    # ── 2. ffmpeg → WAV 16kHz mono ──────────────────────────────────────────
    wav_path = job_out_dir / "audio.wav"
    try:
        ffmpeg_to_wav(Path(input_file), wav_path)
    except Exception as e:
        update_job(job_id, status="FAILED", step="FFMPEG_FAILED", error=str(e))
        pg_update(job_id, status="FAILED", error_message=f"ffmpeg: {e}")
        return

    # ── 3. Trascrizione faster-whisper ──────────────────────────────────────
    update_job(job_id, step="TRANSCRIBING")
    try:
        segments, detected_language = transcribe(fw_model, wav_path, language)
        print(f"[worker] lingua rilevata: {detected_language} — {len(segments)} segmenti", flush=True)
    except Exception as e:
        update_job(job_id, status="FAILED", step="TRANSCRIBE_FAILED", error=str(e))
        pg_update(job_id, status="FAILED", error_message=f"transcription: {e}")
        return

    # ── 4. Diarizzazione pyannote (opzionale) ────────────────────────────────
    if diarize == "true" and diar_pipeline is not None:
        update_job(job_id, step="DIARIZING")
        try:
            from pyannote.audio.pipelines.utils.hook import Hooks

            def _progress(step_name, step_artifact, file=None, total=None, completed=None):
                """Hook real-time: stampa progresso e aggiorna Redis ad ogni step pyannote."""
                if total and completed is not None and total > 0:
                    pct = int(completed / total * 100)
                    print(f"[worker] diarizzazione {step_name}: {pct}%", flush=True)
                    update_job(job_id, step=f"DIARIZING_{step_name.upper()}_{pct}pct")

            with Hooks(_progress) as hook:
                diarization = diar_pipeline(
                    str(wav_path),
                    max_speakers=max_spk,
                    hook=hook,
                )
            segments = merge_transcript_diarization(segments, diarization)
            print(f"[worker] diarizzazione completata", flush=True)
        except Exception as e:
            # Diarizzazione fallita: prosegui senza speaker labels
            print(f"[worker] ⚠ diarizzazione fallita job {job_id}: {e}", flush=True)
            update_job(job_id, step="DIAR_SKIPPED_ERROR")
            segments = [{**s, "speaker": "UNKNOWN"} for s in segments]
    else:
        segments = [{**s, "speaker": "UNKNOWN"} for s in segments]

    # ── 5. Costruisci output ────────────────────────────────────────────────
    update_job(job_id, step="BUILDING_OUTPUT")

    full_text = " ".join(s.get("text", "") for s in segments).strip()
    speakers  = sorted({s.get("speaker", "UNKNOWN") for s in segments})

    now_str   = utc_now()
    minio_url     = ""
    minio_txt_url = ""

    s3 = s3_client()
    ensure_bucket(s3, MINIO_BUCKET)

    # Cartella MinIO: customer/project/YYYYMMDD_HHMMSS_{job_id[:8]}/
    ts           = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    minio_folder = f"{ts}_{job_id[:8]}"

    # ── JSON ─────────────────────────────────────────────────────────────────
    if output_format in ("json", "both"):
        output = {
            "job_id":     job_id,
            "customer":   customer,
            "project":    project,
            "language":   detected_language,
            "speakers":   speakers,
            "segments":   segments,
            "full_text":  full_text,
            "created_at": now_str,
        }
        result_path = job_out_dir / "result.json"
        result_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        minio_key = f"{customer}/{project}/{minio_folder}/result.json"
        try:
            update_job(job_id, step="UPLOADING_JSON")
            s3.upload_file(str(result_path), MINIO_BUCKET, minio_key,
                           ExtraArgs={"ContentType": "application/json"})
            minio_url = f"{MINIO_ENDPOINT}/{MINIO_BUCKET}/{minio_key}"
            print(f"[worker] upload JSON OK: {minio_url}", flush=True)
        except Exception as e:
            print(f"[worker] ⚠ MinIO JSON warning job {job_id}: {e}", flush=True)
            minio_url = f"local:{result_path}"

    # ── TXT ──────────────────────────────────────────────────────────────────
    if output_format in ("txt", "both"):
        txt_lines = [
            f"RIUNIONE:  {customer} / {project}",
            f"DATA:      {now_str[:10]}",
            f"LINGUA:    {detected_language}",
            f"SPEAKER:   {', '.join(speakers)}",
            "",
            "─" * 60,
        ]
        current_speaker = None
        prev_end        = 0.0
        GAP_SEC         = 2.0  # nuovo blocco se gap > 2 sec (anche stesso speaker)

        for seg in segments:
            spk   = seg.get("speaker", "UNKNOWN")
            text  = seg.get("text", "").strip()
            start = seg.get("start", 0.0)
            if not text:
                prev_end = seg.get("end", prev_end)
                continue

            gap         = start - prev_end
            spk_changed = spk != current_speaker
            new_block   = spk_changed or gap > GAP_SEC

            if new_block:
                txt_lines.append("")
                mm = int(start // 60)
                ss = int(start % 60)
                txt_lines.append(f"[{mm:02d}:{ss:02d}] {spk}")
                current_speaker = spk

            txt_lines.append(text)
            prev_end = seg.get("end", start)

        txt_lines += [
            "",
            "─" * 60,
            "",
            "TESTO COMPLETO:",
            full_text,
        ]

        txt_content = "\n".join(txt_lines)
        txt_path    = job_out_dir / "result.txt"
        txt_path.write_text(txt_content, encoding="utf-8")

        minio_txt_key = f"{customer}/{project}/{minio_folder}/result.txt"
        try:
            update_job(job_id, step="UPLOADING_TXT")
            s3.upload_file(str(txt_path), MINIO_BUCKET, minio_txt_key,
                           ExtraArgs={"ContentType": "text/plain; charset=utf-8"})
            minio_txt_url = f"{MINIO_ENDPOINT}/{MINIO_BUCKET}/{minio_txt_key}"
            print(f"[worker] upload TXT OK: {minio_txt_url}", flush=True)
        except Exception as e:
            print(f"[worker] ⚠ MinIO TXT warning job {job_id}: {e}", flush=True)
            minio_txt_url = f"local:{txt_path}"

    # ── 7. Aggiorna Redis e PostgreSQL ───────────────────────────────────────
    update_job(job_id, status="COMPLETED", step="DONE",
               output_json_url=minio_url,
               output_txt_url=minio_txt_url,
               result_path=str(job_out_dir))
    pg_update(job_id, status="COMPLETED",
              output_json_url=minio_url or minio_txt_url,
              finished_at=utc_now())

    print(f"[worker] ✓ job {job_id} COMPLETED", flush=True)
    if minio_url:     print(f"  JSON: {minio_url}", flush=True)
    if minio_txt_url: print(f"  TXT:  {minio_txt_url}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    from faster_whisper import WhisperModel
    from pyannote.audio import Pipeline

    print(f"[worker] Avvio — redis={REDIS_URL} queue={QUEUE_KEY} "
          f"model={WHISPER_MODEL} device={WHISPER_DEVICE}", flush=True)

    # Carica faster-whisper una volta
    print("[worker] Caricamento modello faster-whisper...", flush=True)
    fw_model = WhisperModel(
        WHISPER_MODEL,
        device=WHISPER_DEVICE,
        compute_type=WHISPER_COMPUTE,
    )
    print("[worker] Modello faster-whisper pronto.", flush=True)

    # Carica pyannote una volta
    diar_pipeline = None
    if HF_TOKEN:
        try:
            print("[worker] Caricamento pipeline diarizzazione pyannote...", flush=True)
            diar_pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=HF_TOKEN,
            )
            print("[worker] Pipeline diarizzazione pronta.", flush=True)
        except Exception as e:
            print(f"[worker] ⚠ Diarizzazione non disponibile: {e}", flush=True)
    else:
        print("[worker] ⚠ HF_TOKEN mancante — diarizzazione disabilitata.", flush=True)

    recover_stale_jobs()

    # Loop principale
    while True:
        item = r.blpop(QUEUE_KEY, timeout=BLPOP_TIMEOUT)
        if not item:
            recover_stale_jobs()
            continue

        _, job_id = item
        print(f"[worker] → job {job_id}", flush=True)
        try:
            run_job(job_id, fw_model, diar_pipeline)
        except Exception as e:
            update_job(job_id, status="FAILED", step="WORKER_CRASH", error=str(e))
            pg_update(job_id, status="FAILED", error_message=f"worker crash: {e}")
            print(f"[worker] ✗ crash job {job_id}: {e}", flush=True)


if __name__ == "__main__":
    main()