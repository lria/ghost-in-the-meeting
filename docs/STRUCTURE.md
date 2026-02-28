# Struttura repo consigliata (minima)

Root:
- docker-compose.yml
- Dockerfile.whisperx
- entry_async.py / async_routes.py / async_patch.py
- openapi.json
- .dockerignore
- .gitignore
- .env.example

Cartelle:
- postgres_init/                 # SQL di init (OK su Git)
- webform/                       # HTML (OK su Git)
- nemo/                          # codice e Dockerfile (OK su Git)
- scripts/                       # script operativi (OK su Git)
- docs/                          # documentazione (OK su Git)

Cartelle da NON versionare (runtime):
- n8n_data/, postgres_data/, redis_data/, qdrant_data/, minio_data/, ollama_data/
- data/uploads/, data/nemo/, data/whisper_models/, data/nltk_data/
- whisper_cache/, hf_cache/
- Backup/
