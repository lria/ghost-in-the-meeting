import os
import uuid
import json
import shutil
import subprocess
from pathlib import Path

import redis

import nemo.collections.asr as nemo_asr
from omegaconf import OmegaConf
from nemo.collections.asr.models import ClusteringDiarizer

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_KEY = os.getenv("NEMO_QUEUE_KEY", "nemo:queue")

ASR_MODEL_NAME = os.getenv("NEMO_ASR_MODEL", "stt_multilingual_fastconformer_hybrid_large")
DEVICE = "cpu"

DATA_DIR = Path(os.getenv("NEMO_DATA_DIR", "/data"))
IN_DIR = DATA_DIR / "in"
OUT_DIR = DATA_DIR / "out"
CFG_PATH = Path(os.getenv("NEMO_DIAR_CFG", "/app/diarization.yaml"))
IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def job_key(job_id: str) -> str:
    return f"nemo:job:{job_id}"

def ffmpeg_to_wav(src: Path, dst: Path):
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-vn", str(dst)]
    subprocess.check_call(cmd)

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

def run_job(job_id: str, asr):
    k = job_key(job_id)
    r.hset(k, mapping={"status": "WORKING"})

    input_file = r.hget(k, "input_file")
    max_speakers = int(r.hget(k, "max_speakers") or 8)

    if not input_file or not Path(input_file).exists():
        r.hset(k, mapping={"status": "FAILED", "error": "input_missing", "detail": "input file not found"})
        return

    job_in_dir = IN_DIR / job_id
    job_out_dir = OUT_DIR / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    wav_path = job_in_dir / "audio.wav"
    try:
        ffmpeg_to_wav(Path(input_file), wav_path)
    except Exception as e:
        r.hset(k, mapping={"status": "FAILED", "error": "ffmpeg_failed", "detail": str(e)})
        return

    # ASR
    try:
        transcript_list = asr.transcribe([str(wav_path)])
        transcript = transcript_list[0] if transcript_list else ""
    except Exception as e:
        r.hset(k, mapping={"status": "FAILED", "error": "asr_failed", "detail": str(e)})
        return

    # Diarization
    manifest_path = job_in_dir / "manifest.json"
    manifest = {
        "audio_filepath": str(wav_path),
        "offset": 0,
        "duration": None,
        "label": "infer",
        "text": "-",
        "num_speakers": None,
        "rttm_filepath": str(job_out_dir / "pred.rttm"),
        "uem_filepath": None,
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    cfg = OmegaConf.load(CFG_PATH)
    cfg.diarizer.manifest_filepath = str(manifest_path)
    cfg.diarizer.out_dir = str(job_out_dir)
    cfg.diarizer.clustering.parameters.max_num_speakers = max_speakers

    try:
        diarizer = ClusteringDiarizer(cfg=cfg)
        diarizer.to(DEVICE)
        diarizer.diarize()
    except Exception as e:
        r.hset(k, mapping={"status": "FAILED", "error": "diarization_failed", "detail": str(e)})
        return

    rttm_path = job_out_dir / "pred.rttm"
    speaker_turns = parse_rttm(rttm_path)

    result = {
        "job_id": job_id,
        "transcript": transcript,
        "speaker_turns": speaker_turns,
        "rttm_path": str(rttm_path) if rttm_path.exists() else None,
    }

    result_path = job_out_dir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    r.hset(k, mapping={"status": "COMPLETED", "result_path": str(result_path)})

def main():
    print(f"[worker] redis={REDIS_URL} queue={QUEUE_KEY} model={ASR_MODEL_NAME} device={DEVICE}", flush=True)
    asr = load_asr()
    while True:
        _, job_id = r.blpop(QUEUE_KEY)  # blocking pop
        try:
            run_job(job_id, asr)
        except Exception as e:
            r.hset(job_key(job_id), mapping={"status": "FAILED", "error": "worker_crash", "detail": str(e)})

if __name__ == "__main__":
    main()