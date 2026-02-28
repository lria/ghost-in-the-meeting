#!/usr/bin/env bash
set -euo pipefail

echo "[WARN] This will DELETE local data volumes and caches in the repo folders."
echo "       Repo folders to be removed:"
echo "       - postgres_data/ redis_data/ qdrant_data/ minio_data/ ollama_data/ n8n_data/"
echo "       - data/uploads/ data/nemo/ data/whisper_models/ data/nltk_data/"
echo "       - whisper_cache/ hf_cache/"
echo
read -p "Type 'DELETE' to continue: " CONFIRM
if [[ "${CONFIRM}" != "DELETE" ]]; then
  echo "Aborted."
  exit 1
fi

docker compose down --remove-orphans || true

rm -rf postgres_data redis_data qdrant_data minio_data ollama_data n8n_data       data/uploads data/nemo data/whisper_models data/nltk_data       whisper_cache hf_cache

echo "Done."
