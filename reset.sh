#!/bin/bash
# ============================================================
# reset.sh — Pulizia completa dati di test
# Cancella: job Redis, dati MinIO, righe PostgreSQL
# NON tocca: container, immagini, modelli whisper, configurazione
# ============================================================

set -e

REDIS_CONTAINER="n8n_stack_redis"
POSTGRES_CONTAINER="n8n_stack_postgres"
MINIO_CONTAINER="n8n_stack_minio"
PG_USER="n8n"
PG_DB="n8n"
WX_QUEUE="wx:queue"
WX_BUCKET="wx-transcriptions"

echo "=== RESET dati di test ==="
echo ""

# ── 1. Redis — svuota coda e job ──────────────────────────────────────────────
echo "→ Redis: pulizia coda e job..."
docker exec $REDIS_CONTAINER redis-cli DEL $WX_QUEUE > /dev/null
JOB_KEYS=$(docker exec $REDIS_CONTAINER redis-cli KEYS "wx:job:*")
if [ -n "$JOB_KEYS" ]; then
    echo "$JOB_KEYS" | xargs docker exec $REDIS_CONTAINER redis-cli DEL > /dev/null
    echo "  ✓ job rimossi"
else
    echo "  ✓ nessun job trovato"
fi

# ── 2. PostgreSQL — svuota tabelle ────────────────────────────────────────────
echo "→ PostgreSQL: pulizia tabelle..."
docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d $PG_DB -c \
    "TRUNCATE wx_transcription_jobs, transcription_runs RESTART IDENTITY CASCADE;" > /dev/null
echo "  ✓ tabelle svuotate"

# ── 3. MinIO — svuota bucket ──────────────────────────────────────────────────
echo "→ MinIO: pulizia bucket $WX_BUCKET..."
docker exec $MINIO_CONTAINER sh -c \
    "mc alias set local http://localhost:9000 \$MINIO_ROOT_USER \$MINIO_ROOT_PASSWORD > /dev/null 2>&1 && \
     mc rb --force local/$WX_BUCKET > /dev/null 2>&1 && \
     mc mb local/$WX_BUCKET > /dev/null 2>&1" || \
    echo "  ⚠ bucket non esisteva o già vuoto"
echo "  ✓ bucket ricreato vuoto"

# ── 4. File audio su disco ────────────────────────────────────────────────────
echo "→ Disco: pulizia file audio e output..."
UPLOADS_DIR="./data/uploads"
if [ -d "$UPLOADS_DIR/in" ]; then
    rm -rf "$UPLOADS_DIR/in"/*
    echo "  ✓ file input rimossi"
fi
if [ -d "$UPLOADS_DIR/out" ]; then
    rm -rf "$UPLOADS_DIR/out"/*
    echo "  ✓ file output rimossi"
fi

echo ""
echo "=== Reset completato ==="
echo "    Modelli Whisper e pyannote: intatti (./data/whisper_models)"
echo "    Container e immagini:       intatti"
echo "    Configurazione:             intatta"
