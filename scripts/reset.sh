#!/usr/bin/env bash
# =============================================================================
# reset.sh — Pulisce tutti i dati di test senza toccare modelli e configurazione
#
# Cosa azzera:
#   • Redis       — tutti i job (wx:job:*, wx:queue)
#   • PostgreSQL  — righe in wx_transcription_jobs, speaker_aliases, minutes_jobs
#   • MinIO       — tutti i file nel bucket wx-transcriptions
#   • Disco       — file audio e output in ./data/uploads/
#
# Cosa NON tocca:
#   • Modelli Whisper/pyannote (./data/whisper_models)
#   • Modelli Ollama           (./data/ollama_data o volume Docker)
#   • Configurazione n8n       (./n8n_data)
#   • Configurazione Docker e .env
#   • Dati Qdrant              (vettori RAG) — usa --qdrant per includerli
#
# Uso:
#   ./reset.sh              reset standard
#   ./reset.sh --qdrant     reset + svuota collezione Qdrant
#   ./reset.sh --all        reset + qdrant + conferma automatica (CI/CD)
# =============================================================================

set -euo pipefail

# ── Colori ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Argomenti ─────────────────────────────────────────────────────────────────
RESET_QDRANT=false
AUTO_YES=false
for arg in "$@"; do
  case $arg in
    --qdrant) RESET_QDRANT=true ;;
    --all)    RESET_QDRANT=true; AUTO_YES=true ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║        Ghost in the Meeting — Reset      ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${CYAN}Redis${RESET}      job queue + job state"
echo -e "  ${CYAN}PostgreSQL${RESET} wx_transcription_jobs, speaker_aliases, minutes_jobs"
echo -e "  ${CYAN}MinIO${RESET}      bucket wx-transcriptions"
echo -e "  ${CYAN}Disco${RESET}      ./data/uploads/"
if $RESET_QDRANT; then
echo -e "  ${YELLOW}Qdrant${RESET}     collezione meeting_minutes ${YELLOW}(--qdrant attivo)${RESET}"
fi
echo ""

# ── Conferma ──────────────────────────────────────────────────────────────────
if ! $AUTO_YES; then
  echo -e "${YELLOW}⚠ Questa operazione è irreversibile.${RESET}"
  read -rp "  Continuare? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Annullato."; exit 0; }
  echo ""
fi

OK="${GREEN}✓${RESET}"
FAIL="${RED}✗${RESET}"

# ── 1. Redis ──────────────────────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Redis${RESET}"

redis_jobs=$(docker exec n8n_stack_redis \
  redis-cli --scan --pattern "wx:job:*" 2>/dev/null | wc -l | tr -d ' ')

if [[ "$redis_jobs" -gt 0 ]]; then
  docker exec n8n_stack_redis \
    redis-cli --scan --pattern "wx:job:*" | \
    xargs docker exec -i n8n_stack_redis redis-cli DEL > /dev/null
fi

docker exec n8n_stack_redis redis-cli DEL wx:queue > /dev/null 2>&1 || true

echo -e "  $OK rimossi ${redis_jobs} job + coda"

# ── 2. PostgreSQL ─────────────────────────────────────────────────────────────
echo -e "${BOLD}[2/5] PostgreSQL${RESET}"

PG_USER="${POSTGRES_USER:-n8n}"
PG_DB="${POSTGRES_DB:-n8n}"

pg_run() {
  docker exec n8n_stack_postgres \
    psql -U "$PG_USER" -d "$PG_DB" -t -c "$1" 2>/dev/null | tr -d ' \n'
}

# Conta prima
wx_count=$(pg_run "SELECT COUNT(*) FROM wx_transcription_jobs;" 2>/dev/null || echo "?")
sp_count=$(pg_run "SELECT COUNT(*) FROM speaker_aliases;"       2>/dev/null || echo "?")
mn_count=$(pg_run "SELECT COUNT(*) FROM minutes_jobs;"          2>/dev/null || echo "?")

docker exec n8n_stack_postgres \
  psql -U "$PG_USER" -d "$PG_DB" -c "
    TRUNCATE minutes_jobs, speaker_aliases, wx_transcription_jobs
    RESTART IDENTITY CASCADE;
  " > /dev/null 2>&1

echo -e "  $OK wx_transcription_jobs: ${wx_count} righe"
echo -e "  $OK speaker_aliases:       ${sp_count} righe"
echo -e "  $OK minutes_jobs:          ${mn_count} righe"

# ── 3. MinIO ──────────────────────────────────────────────────────────────────
echo -e "${BOLD}[3/5] MinIO — bucket wx-transcriptions${RESET}"

MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-minioadmin}"

minio_count=$(docker exec n8n_stack_minio sh -c "
  mc alias set local http://localhost:9000 ${MINIO_USER} ${MINIO_PASS} > /dev/null 2>&1 &&
  mc ls --recursive local/wx-transcriptions 2>/dev/null | wc -l
" | tr -d ' ') || minio_count="?"

docker exec n8n_stack_minio sh -c "
  mc alias set local http://localhost:9000 ${MINIO_USER} ${MINIO_PASS} > /dev/null 2>&1 &&
  mc rm --recursive --force local/wx-transcriptions > /dev/null 2>&1 || true
" 2>/dev/null || true

echo -e "  $OK rimossi ${minio_count} file"

# ── 4. Disco — file audio e output ────────────────────────────────────────────
echo -e "${BOLD}[4/5] Disco — ./data/uploads/${RESET}"

disk_freed=0
for dir in ./data/uploads/in ./data/uploads/out; do
  if [[ -d "$dir" ]]; then
    size=$(du -sm "$dir" 2>/dev/null | cut -f1 || echo 0)
    disk_freed=$((disk_freed + size))
    find "$dir" -mindepth 1 -delete 2>/dev/null || true
    echo -e "  $OK svuotato $dir (${size} MB)"
  else
    echo -e "  — $dir non trovato, skip"
  fi
done
echo -e "  $OK spazio liberato: ~${disk_freed} MB"

# ── 5. Qdrant (opzionale) ─────────────────────────────────────────────────────
echo -e "${BOLD}[5/5] Qdrant${RESET}"

if $RESET_QDRANT; then
  qdrant_resp=$(curl -s -o /dev/null -w "%{http_code}" \
    -X DELETE "http://localhost:6333/collections/meeting_minutes" 2>/dev/null || echo "000")
  if [[ "$qdrant_resp" == "200" ]]; then
    echo -e "  $OK collezione meeting_minutes eliminata"
  elif [[ "$qdrant_resp" == "404" ]]; then
    echo -e "  — collezione non esisteva, nulla da fare"
  else
    echo -e "  ${YELLOW}⚠ risposta inattesa: HTTP ${qdrant_resp}${RESET}"
  fi
else
  echo -e "  — skip (usa ${CYAN}--qdrant${RESET} per includere i vettori RAG)"
fi

# ── Fine ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Reset completato.${RESET}"
echo ""
echo -e "  Stack ancora in esecuzione — nessun container riavviato."
echo -e "  Per un fresh start completo: ${CYAN}docker compose restart${RESET}"
echo ""