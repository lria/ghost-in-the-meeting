"""
wx_cleanup.py — Cleanup Worker
Cancella periodicamente i file temporanei su disco (input audio + output locale)
per i job già completati o falliti su MinIO.

I job in stato PAUSED NON vengono mai toccati: il file audio di input è
necessario per poter riprendere il job via POST /jobs/{id}/resume.

Parametri via env:
  WX_CLEANUP_INTERVAL_SEC   intervallo tra ogni ciclo di pulizia (default: 3600 = 1h)
  WX_CLEANUP_MIN_AGE_SEC    età minima del job prima di pulire (default: 300 = 5 min)
  WX_DATA_DIR               cartella dati (default: /data)
  REDIS_URL                 URL Redis
"""

import os
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

import redis

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL       = os.getenv("REDIS_URL",                  "redis://redis:6379/0")
DATA_DIR        = Path(os.getenv("WX_DATA_DIR",           "/data"))
IN_DIR          = DATA_DIR / "in"
OUT_DIR         = DATA_DIR / "out"

INTERVAL_SEC    = int(os.getenv("WX_CLEANUP_INTERVAL_SEC", str(60 * 60)))   # 1h
MIN_AGE_SEC     = int(os.getenv("WX_CLEANUP_MIN_AGE_SEC",  str(5 * 60)))    # 5 min

# ── Client ────────────────────────────────────────────────────────────────────

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def job_age_sec(job_id: str) -> float:
    """Restituisce l'età in secondi dell'ultimo aggiornamento del job su Redis."""
    updated_at = r.hget(f"wx:job:{job_id}", "updated_at") or ""
    if not updated_at:
        return float("inf")
    try:
        ts  = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")

def is_terminal(job_id: str) -> bool:
    """
    Restituisce True se il job è in stato finale pulibile (COMPLETED o FAILED).
    PAUSED non è terminale: il file di input serve per poter fare resume.
    """
    status = r.hget(f"wx:job:{job_id}", "status") or ""
    return status in ("COMPLETED", "FAILED")

def cleanup_job(job_id: str) -> dict:
    """
    Cancella le cartelle input e output su disco per un job.
    Restituisce un report delle operazioni eseguite.
    """
    report = {"job_id": job_id, "deleted": [], "skipped": [], "errors": []}

    for label, path in [("input", IN_DIR / job_id), ("output", OUT_DIR / job_id)]:
        if path.exists():
            try:
                shutil.rmtree(path)
                report["deleted"].append(str(path))
            except Exception as e:
                report["errors"].append(f"{label}: {e}")
        else:
            report["skipped"].append(str(path))

    return report

# ── Ciclo pulizia ─────────────────────────────────────────────────────────────

def run_cleanup():
    """Esegue un ciclo completo di pulizia."""
    print(f"[cleanup] Avvio ciclo pulizia — {datetime.now(timezone.utc).isoformat()}", flush=True)

    cleaned   = 0
    skipped   = 0
    errors    = 0
    freed_mb  = 0.0

    # Calcola spazio occupato prima della pulizia
    for dirpath in [IN_DIR, OUT_DIR]:
        if dirpath.exists():
            for f in dirpath.rglob("*"):
                if f.is_file():
                    freed_mb += f.stat().st_size / (1024 * 1024)

    # Scansiona tutti i job su Redis
    for key in r.scan_iter(match="wx:job:*", count=200):
        job_id = r.hget(key, "job_id") or key.split(":")[-1]
        status = r.hget(f"wx:job:{job_id}", "status") or "UNKNOWN"

        if not is_terminal(job_id):
            print(f"[cleanup] skip {job_id[:8]}... — stato {status}, non terminale", flush=True)
            skipped += 1
            continue

        age = job_age_sec(job_id)
        if age < MIN_AGE_SEC:
            print(f"[cleanup] skip {job_id[:8]}... — troppo recente ({int(age)}s < {MIN_AGE_SEC}s)", flush=True)
            skipped += 1
            continue

        report = cleanup_job(job_id)

        if report["deleted"]:
            print(f"[cleanup] ✓ {job_id[:8]}... — rimosso: {', '.join(Path(d).name for d in report['deleted'])}", flush=True)
            cleaned += 1
        if report["errors"]:
            print(f"[cleanup] ✗ {job_id[:8]}... — errori: {report['errors']}", flush=True)
            errors += 1

    # Ricalcola spazio dopo pulizia
    freed_after = 0.0
    for dirpath in [IN_DIR, OUT_DIR]:
        if dirpath.exists():
            for f in dirpath.rglob("*"):
                if f.is_file():
                    freed_after += f.stat().st_size / (1024 * 1024)

    freed = freed_mb - freed_after
    print(
        f"[cleanup] Ciclo completato — "
        f"puliti: {cleaned}, saltati: {skipped}, errori: {errors}, "
        f"spazio liberato: {freed:.1f} MB",
        flush=True
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(
        f"[cleanup] Avvio — intervallo: {INTERVAL_SEC}s, "
        f"età minima job: {MIN_AGE_SEC}s, "
        f"data_dir: {DATA_DIR}",
        flush=True
    )
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            run_cleanup()
        except Exception as e:
            print(f"[cleanup] ✗ errore ciclo: {e}", flush=True)
        print(f"[cleanup] prossimo ciclo tra {INTERVAL_SEC}s", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()