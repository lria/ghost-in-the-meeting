import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import redis

import torch
torch.set_num_threads(1)
torch.set_num_interop_threads(1)

import nemo.collections.asr as nemo_asr
from omegaconf import OmegaConf
from nemo.collections.asr.models import ClusteringDiarizer

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_KEY = os.getenv("NEMO_QUEUE_KEY", "nemo:queue")

ASR_MODEL_NAME = os.getenv("NEMO_ASR_MODEL", "stt_multilingual_fastconformer_hybrid_large_pc")
DEVICE = "cpu"

CHUNK_SEC = int(os.getenv("NEMO_CHUNK_SEC", "30"))  # split audio for ASR to avoid OOM
MAX_ATTEMPTS = int(os.getenv("NEMO_MAX_ATTEMPTS", "3"))

# Recovery / polling
BLPOP_TIMEOUT_SEC = int(os.getenv("NEMO_BLPOP_TIMEOUT_SEC", "5"))
# Mark WORKING jobs as stale if not updated for this many seconds (default 30 min)
STALE_WORKING_SEC = int(os.getenv("NEMO_STALE_WORKING_SEC", str(30 * 60)))

DATA_DIR = Path(os.getenv("NEMO_DATA_DIR", "/data"))
IN_DIR = DATA_DIR / "in"
OUT_DIR = DATA_DIR / "out"
CFG_PATH = Path(os.getenv("NEMO_DIAR_CFG", "/app/diarization.yaml"))

IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_key(job_id: str) -> str:
    return f"nemo:job:{job_id}"


def update_job(job_id: str, **fields):
    fields["updated_at"] = utc_now()
    r.hset(job_key(job_id), mapping={k: str(v) for k, v in fields.items()})


def ffmpeg_to_wav(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-vn", str(dst)]
    subprocess.check_call(cmd)

def split_wav(src_wav: Path, chunks_dir: Path, chunk_sec: int) -> list[Path]:
    """Split a wav into chunks to keep ASR memory bounded."""
    chunks_dir.mkdir(parents=True, exist_ok=True)
    # Example output: chunk_000.wav, chunk_001.wav, ...
    out_pattern = str(chunks_dir / "chunk_%03d.wav")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_wav),
        "-f", "segment",
        "-segment_time", str(chunk_sec),
        "-reset_timestamps", "1",
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_pattern,
    ]
    subprocess.check_call(cmd)
    return sorted(chunks_dir.glob("chunk_*.wav"))

def load_asr():
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=ASR_MODEL_NAME)
    model.to(DEVICE)
    model.eval()
    return model


def parse_rttm(rttm_path: Path):
    turns = []
    if not rttm_path.exists():
        return turns
    for line in rttm_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 9 and parts[0] == "SPEAKER":
            start = float(parts[3])
            dur = float(parts[4])
            spk = parts[7]
            turns.append({"speaker": spk, "start": start, "end": start + dur})
    return turns


def recover_stale_jobs():
    """
    If the worker restarts mid-job, Redis will still show WORKING.
    This function requeues stale WORKING jobs and any PENDING jobs not yet processed.
    """
    for key in r.scan_iter(match="nemo:job:*", count=200):
        status = r.hget(key, "status") or ""
        job_id = r.hget(key, "job_id") or key.split(":")[-1]
        input_file = r.hget(key, "input_file") or ""

        if not input_file or not Path(input_file).exists():
            continue

        if status == "PENDING":
            r.rpush(QUEUE_KEY, job_id)
            update_job(job_id, step="RECOVERY_REQUEUED_PENDING")
            continue

        if status == "WORKING":
            updated_at = r.hget(key, "updated_at") or ""
            try:
                if updated_at:
                    ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                else:
                    age = STALE_WORKING_SEC + 1
            except Exception:
                age = STALE_WORKING_SEC + 1

            if age >= STALE_WORKING_SEC:
                r.rpush(QUEUE_KEY, job_id)
                update_job(job_id, status="PENDING", step="RECOVERY_REQUEUED_STALE_WORKING")


def run_job(job_id: str, asr):
    k = job_key(job_id)

    input_file = r.hget(k, "input_file")
    max_speakers = int(r.hget(k, "max_speakers") or 8)

    if not input_file or not Path(input_file).exists():
        update_job(job_id, status="FAILED", step="INPUT_CHECK", error="input_missing", detail="input file not found")
        return

    job_in_dir = IN_DIR / job_id
    job_out_dir = OUT_DIR / job_id
    job_in_dir.mkdir(parents=True, exist_ok=True)
    job_out_dir.mkdir(parents=True, exist_ok=True)

    update_job(job_id, status="WORKING", step="FFMPEG_START")

    wav_path = job_in_dir / "audio.wav"
    try:
        ffmpeg_to_wav(Path(input_file), wav_path)
    except Exception as e:
        update_job(job_id, status="FAILED", step="FFMPEG_FAILED", error="ffmpeg_failed", detail=str(e))
        return

    update_job(job_id, step="ASR_START")

    try:
        transcript_list = asr.transcribe([str(wav_path)])
        transcript = transcript_list[0] if transcript_list else ""
    except Exception as e:
        update_job(job_id, status="FAILED", step="ASR_FAILED", error="asr_failed", detail=str(e))
        return

    update_job(job_id, step="DIAR_START")

    manifest_path = job_in_dir / "manifest.json"
    rttm_path = job_out_dir / "pred.rttm"

    manifest = {
        "audio_filepath": str(wav_path),
        "offset": 0,
        "duration": None,
        "label": "infer",
        "text": "-",
        "num_speakers": None,
        "rttm_filepath": str(rttm_path),
        "uem_filepath": None,
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    try:
        cfg = OmegaConf.load(CFG_PATH)
        cfg.diarizer.manifest_filepath = str(manifest_path)
        cfg.diarizer.out_dir = str(job_out_dir)
        cfg.diarizer.clustering.parameters.max_num_speakers = max_speakers

        diarizer = ClusteringDiarizer(cfg=cfg)
        diarizer.to(DEVICE)
        diarizer.diarize()
    except Exception as e:
        update_job(job_id, status="FAILED", step="DIAR_FAILED", error="diarization_failed", detail=str(e))
        return

    update_job(job_id, step="WRITE_RESULT")

    speaker_turns = parse_rttm(rttm_path)

    result = {
        "job_id": job_id,
        "transcript": transcript,
        "speaker_turns": speaker_turns,
        "rttm_path": str(rttm_path) if rttm_path.exists() else None,
    }

    result_path = job_out_dir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    update_job(job_id, status="COMPLETED", step="DONE", result_path=str(result_path))


def main():
    print(f"[worker] redis={REDIS_URL} queue={QUEUE_KEY} model={ASR_MODEL_NAME} device={DEVICE}", flush=True)

    recover_stale_jobs()
    asr = load_asr()

    while True:
        item = r.blpop(QUEUE_KEY, timeout=BLPOP_TIMEOUT_SEC)
        if not item:
            recover_stale_jobs()
            continue

        _, job_id = item
        try:
            run_job(job_id, asr)
        except Exception as e:
            update_job(job_id, status="FAILED", step="WORKER_CRASH", error="worker_crash", detail=str(e))


if __name__ == "__main__":
    main()
