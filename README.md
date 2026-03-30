# 👻 Ghost in the Meeting

> *Il fantasma che vuoi davvero nelle tue riunioni.*

> **Configurazione rapida:** tutti i parametri dell'applicazione (URL dei servizi, modelli, template di default, lingua, ecc.) si possono impostare direttamente dall'interfaccia web. Aprire `http://localhost:8081` e navigare alla sezione **Settings** nel menu principale per accedere al pannello di configurazione completo senza modificare file o variabili d'ambiente.

Un sistema self-hosted, completamente locale e asincrono per trascrivere audio di riunioni, identificare i parlanti e generare verbali strutturati tramite intelligenza artificiale — senza inviare dati a servizi cloud.

---

## Indice

1. [Finalità del progetto](#finalità-del-progetto)
2. [Prerequisiti](#prerequisiti)
3. [Installazione passo dopo passo](#installazione-passo-dopo-passo)
4. [Moduli funzionali e mapping Docker](#moduli-funzionali-e-mapping-docker)
5. [Guida alle pagine web](#guida-alle-pagine-web)
   - [Pagina Trascrizione](#pagina-trascrizione)
   - [Pagina Associazione Speaker](#pagina-associazione-speaker)
   - [Pagina Generazione Minuta](#pagina-generazione-minuta)
6. [Installare nuovi template](#installare-nuovi-template)
7. [Installare nuovi modelli con Ollama](#installare-nuovi-modelli-con-ollama)
8. [Riferimento API](#riferimento-api)
9. [Comandi utili](#comandi-utili)
10. [Performance](#performance)
11. [Roadmap](#roadmap)

---

## Finalità del progetto

**Ghost in the Meeting** risolve un problema pratico e frequente: le riunioni aziendali producono informazioni preziose che si perdono senza una documentazione accurata. Trascrivere manualmente è lento, delegare a servizi cloud espone dati sensibili.

Il progetto offre una pipeline completamente locale che:

- **Trascrive** audio di riunioni in italiano e inglese usando [faster-whisper](https://github.com/SYSTRAN/faster-whisper), un'implementazione ottimizzata di OpenAI Whisper
- **Diarizza i parlanti** tramite [pyannote.audio](https://github.com/pyannote/pyannote-audio), distinguendo automaticamente chi sta parlando e quando
- **Rinomina i parlanti** permettendo di associare etichette anonime (`SPEAKER_00`, `SPEAKER_01`) ai nomi reali dei partecipanti
- **Genera verbali** in formato strutturato usando un LLM locale (Ollama) con RAG (Retrieval-Augmented Generation) su Qdrant, applicando template personalizzabili
- **Archivia tutto** su MinIO (object storage locale compatibile S3) con path organizzati per cliente e progetto

Il sistema è progettato per **Apple Silicon (M1/M2/M3)** e funziona interamente su Docker, senza richiedere GPU.

---

## Prerequisiti

| Requisito | Dettaglio |
|---|---|
| **Docker Desktop** | Versione recente per Mac Apple Silicon |
| **RAM assegnata a Docker** | Minimo **10 GB** (Impostazioni → Risorse → Memoria) |
| **Account HuggingFace** | Necessario per scaricare i modelli di diarizzazione |
| **Token HuggingFace** | Con licenza accettata per i modelli pyannote (vedi sotto) |

### Accettare le licenze HuggingFace

Prima di avviare lo stack, accedere a HuggingFace e accettare i termini per:

- [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0)

Poi generare un token su [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) e annotarlo: servirà nel file `.env`.

---

## Installazione passo dopo passo

### Passo 1 — Clonare il repository

```bash
git clone https://github.com/lria/ghost-in-the-meeting
cd ghost-in-the-meeting
```

### Passo 2 — Creare e configurare il file `.env`

```bash
cp .env.example .env
```

Aprire `.env` con un editor e compilare tutte le variabili:

```dotenv
# ── PostgreSQL ──────────────────────────────────────────────
POSTGRES_USER=n8n
POSTGRES_PASSWORD=scegli_una_password_sicura
POSTGRES_DB=n8n

# ── MinIO (object storage) ──────────────────────────────────
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=scegli_una_password_sicura

# ── n8n (orchestrazione workflow) ──────────────────────────
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=scegli_una_password_sicura
N8N_ENCRYPTION_KEY=una_chiave_di_32_caratteri_esatti

# ── HuggingFace (modelli pyannote) ─────────────────────────
HF_TOKEN=hf_il_tuo_token_huggingface

# ── Timezone ────────────────────────────────────────────────
TZ=Europe/Rome
```

> **Nota su `N8N_ENCRYPTION_KEY`**: deve essere esattamente 32 caratteri. Generarne uno con:
> ```bash
> openssl rand -hex 16
> ```

### Passo 3 — Avviare lo stack Docker

```bash
docker compose up -d
```

Il primo avvio richiede **circa 10 minuti**: Docker scarica le immagini base, compila i layer Python, e il worker WhisperX scarica i modelli faster-whisper e pyannote da HuggingFace (~2–4 GB totali).

Per monitorare il progresso:

```bash
docker logs n8n_whisperx_worker --follow
```

Lo stack è pronto quando il worker stampa `Worker ready. Waiting for jobs...`.

### Passo 4 — Migrazioni del database

> **Prima installazione:** questo passo **non è necessario**. La directory `postgres_init/` è montata come `docker-entrypoint-initdb.d` nel container PostgreSQL, quindi tutti gli script SQL vengono eseguiti automaticamente da Docker al primo avvio (quando il volume dati è vuoto).
>
> **Aggiornamento da installazione esistente:** se il database è già stato inizializzato in precedenza e mancano gli stati `PAUSED` o `DELETED`, applicare manualmente solo le migrazioni mancanti:
>
> ```bash
> docker exec -i n8n_stack_postgres psql -U n8n -d n8n \
>   < postgres_init/003_add_paused_status.sql
>
> docker exec -i n8n_stack_postgres psql -U n8n -d n8n \
>   < postgres_init/004_add_deleted_status.sql
> ```

### Passo 5 — Configurare MinIO (bucket pubblico)

Il bucket `wx-transcriptions` deve essere accessibile in lettura anonima perché l'interfaccia web possa scaricare i risultati:

```bash
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD &&
  mc mb --ignore-existing local/wx-transcriptions &&
  mc mb --ignore-existing local/minutes &&
  mc anonymous set download local/wx-transcriptions &&
  mc anonymous set download local/minutes
'
```

### Passo 6 — Installare il modello di embedding per RAG

> **Alternativa da UI:** i passi 6 e 7 possono essere eseguiti direttamente dalla pagina Generazione Minuta (`http://localhost:8081/minutes.html`) nella sezione di configurazione modelli, senza usare il terminale.

Il sistema usa `nomic-embed-text` per indicizzare le trascrizioni su Qdrant. Installarlo in Ollama:

```bash
docker exec -it n8n_stack_ollama ollama pull nomic-embed-text
```

### Passo 7 — Installare un modello LLM per la generazione dei verbali

Installare almeno un modello di testo. Per iniziare, il consiglio è `mistral` (7B, buon equilibrio qualità/velocità su CPU):

```bash
docker exec -it n8n_stack_ollama ollama pull mistral
```

### Passo 8 — Verificare che tutto sia attivo

Aprire il browser su [http://localhost:8081](http://localhost:8081). La Web UI deve essere visibile.

Verificare gli altri servizi:

```bash
# Whisper API
curl http://localhost:9200/health

# SSE Broker
curl http://localhost:9400/jobs

# Minutes API
curl http://localhost:9600/health
```

---

## Moduli funzionali e mapping Docker

Il sistema è composto da **tre aree funzionali** — Trascrizione, Archiviazione, Generazione Minuta — più infrastruttura di supporto.

### Area 1 — Trascrizione audio

| Modulo funzionale | Container Docker | Porta | Ruolo |
|---|---|---|---|
| **API Trascrizione** | `n8n_whisperx_api` | `9200` | Riceve upload audio, crea job su Redis, espone polling stato |
| **Worker Trascrizione** | `n8n_whisperx_worker` | — | Esegue faster-whisper + pyannote, produce JSON/TXT, carica su MinIO |
| **Cleanup Worker** | `n8n_whisperx_cleanup` | — | Cancella file temporanei su disco ogni ora (non tocca MinIO) |

Il worker carica i modelli **una sola volta** all'avvio e rimane in ascolto sulla coda Redis. Questo evita reload costosi a ogni job.

Pipeline di elaborazione per ogni job:

```
ffmpeg → WAV 16kHz mono
  └→ faster-whisper → trascrizione con timestamp per parola
      └→ pyannote → diarizzazione parlanti (opzionale)
          └→ merge → assegnazione speaker a ogni segmento
              └→ MinIO → upload result.json e/o result.txt
```

### Area 2 — Gestione trascrizioni e speaker

| Modulo funzionale | Container Docker | Porta | Ruolo |
|---|---|---|---|
| **Transcriber API** | `n8n_transcriber_api` | `9500` | Lettura trascrizioni da MinIO, rename speaker, gestione alias |

Questo servizio (ex `minutes_api`) si occupa di tutto ciò che riguarda la trascrizione già prodotta: leggere il testo, rinominare i parlanti, memorizzare gli alias speaker su PostgreSQL.

### Area 3 — Generazione verbale (AI)

| Modulo funzionale | Container Docker | Porta | Ruolo |
|---|---|---|---|
| **Minutes API** | `n8n_minutes_api` | `9600` | Crea job di generazione verbale, espone stato |
| **Minutes Worker** | `n8n_minutes_worker` | — | RAG su Qdrant + prompt Ollama + upload minuta su MinIO |
| **RAG Indexer** | `n8n_rag_indexer` | — | Chunking + embedding + upsert su Qdrant al termine di ogni trascrizione |

### Infrastruttura di supporto

| Servizio | Container Docker | Porta | Ruolo |
|---|---|---|---|
| **Web UI** | `n8n_stack_webform` | `8081` | Interfaccia utente (nginx che serve HTML/CSS/JS statici) |
| **SSE Broker** | `n8n_sse_broker` | `9400` | Polling Redis ogni 2s → push aggiornamenti al browser via Server-Sent Events |
| **n8n** | `n8n_stack` | `5678` | Orchestrazione workflow (ingestion webhook, automazioni) |
| **PostgreSQL** | `n8n_stack_postgres` | — | Metadata job, alias speaker, storico minute |
| **Redis** | `n8n_stack_redis` | — | Code FIFO (`wx:queue`, `minutes:queue`, `rag:queue`) e stato job |
| **MinIO** | `n8n_stack_minio` | `9000`/`9001` | Archiviazione file (audio, trascrizioni JSON/TXT, minute) |
| **Qdrant** | `n8n_stack_qdrant` | `6333` | Vector store per RAG sulle trascrizioni |
| **Ollama** | `n8n_stack_ollama` | `11434` | LLM locale per embedding e generazione verbale |

### Mappa delle dipendenze

```
Browser (8081)
    │
    ├─[caricamento pagina]─→ SSE Broker (9400) ─→ Redis
    │                              └─[eventi]─────→ Browser
    │
    ├─[upload audio]──────→ WhisperX API (9200) ─→ Redis queue
    │                                               └─→ Worker ─→ MinIO
    │
    ├─[rename speaker]────→ Transcriber API (9500) ─→ PostgreSQL + MinIO
    │
    └─[genera minuta]─────→ Minutes API (9600) ─→ Redis queue
                                                    └─→ Worker ─→ Qdrant + Ollama ─→ MinIO
```

---

## Guida alle pagine web

L'interfaccia è accessibile su **[http://localhost:8081](http://localhost:8081)** ed è composta da tre sezioni principali.

---

### Pagina Trascrizione

**URL:** `http://localhost:8081` (pagina principale)

La pagina è divisa in due colonne:

#### Colonna sinistra — Nuova Trascrizione

Permette di avviare un nuovo job di trascrizione compilando un form:

| Campo | Tipo | Descrizione |
|---|---|---|
| **File audio** | Drag & drop / click | Formati supportati: mp3, m4a, wav, mp4, ogg, flac |
| **Cliente** | Testo libero | Usato come namespace nel path MinIO (`cliente/progetto/...`) |
| **Progetto** | Testo libero | Secondo livello del path MinIO |
| **Lingua** | Selettore | `it` (italiano), `en` (inglese), `auto` (rilevamento automatico) |
| **Numero massimo di parlanti** | Numero (1–20) | Limita il numero di speaker che pyannote cerca |
| **Diarizzazione** | Toggle | Abilita/disabilita l'identificazione dei parlanti |
| **Formato output** | Radio | `JSON`, `TXT`, oppure `entrambi` |

Dopo aver cliccato **Avvia Trascrizione**, il file viene inviato all'API WhisperX (`POST /jobs`) e il job entra in coda Redis.

#### Colonna destra — Stato Trascrizioni

**Trascrizione Corrente** — per il job attivo:
- Barra di avanzamento con percentuale
- Passi animati della pipeline (`CONVERTING`, `TRANSCRIBING`, `DIARIZING_EMBEDDINGS_42pct`, ecc.)
- Pulsante **Sospendi** — mette il job in stato `PAUSED`; il file audio viene conservato su disco per consentire la ripresa
- Pulsante **Elimina** — scoping a scelta (audio / trascrizione / rag / purge)

**Storico Trascrizioni** — accordion con tutti i job precedenti:
- Badge di stato colorati (`COMPLETED`, `FAILED`, `PAUSED`, `DELETED`)
- Link diretti ai file su MinIO per download
- Azioni inline: **Associa Speaker**, **Genera Minuta**, **Riprova**, **Elimina**

Gli aggiornamenti arrivano in tempo reale tramite **SSE** (Server-Sent Events): il browser apre una connessione persistente verso `http://localhost:9400/events?all=1` e riceve eventi `job_update` ogni volta che Redis cambia stato.

---

### Pagina Associazione Speaker

**URL:** `http://localhost:8081/speakers.html` (accessibile anche dal pulsante "Associa Speaker" nello storico)

Questa pagina consente di sostituire le etichette anonime generate da pyannote (`SPEAKER_00`, `SPEAKER_01`, ...) con i nomi reali dei partecipanti alla riunione.

#### Flusso operativo

1. **Caricamento della trascrizione** — La pagina interroga la Transcriber API (`GET /transcriptions/{job_id}`) per recuperare il JSON della trascrizione da MinIO.

2. **Visualizzazione dei segmenti** — I segmenti della trascrizione sono mostrati raggruppati per speaker. Accanto a ogni etichetta (`SPEAKER_00`) compare un campo di testo.

3. **Ascolto dei segmenti** — Per ogni speaker è disponibile un pulsante play che riproduce l'audio del primo segmento, aiutando a identificare la voce.

4. **Compilazione dei nomi** — L'utente inserisce il nome reale accanto a ogni `SPEAKER_XX`.

5. **Salvataggio** — Il click su **Salva Associazioni** invia i mapping alla Transcriber API (`POST /transcriptions/{job_id}/speakers`), che:
   - Aggiorna `speaker_aliases` su PostgreSQL
   - Rigenera il file TXT su MinIO con i nomi reali al posto delle etichette
   - Aggiorna il JSON con il campo `speaker_name` per ogni segmento

#### Dove vengono salvati i dati

Gli alias speaker vengono persistiti nella tabella `speaker_aliases` di PostgreSQL, collegata al `job_id`. Sono disponibili in tutti i passi successivi (generazione minuta, esportazione).

---

### Pagina Generazione Minuta

**URL:** `http://localhost:8081/minutes.html` (accessibile anche dal pulsante "Genera Minuta" nello storico)

Questa pagina avvia la generazione di un verbale strutturato usando il LLM locale (Ollama) con recupero contestuale dal vector store (RAG su Qdrant).

#### Flusso operativo

1. **Selezione del template** — Un menu a tendina mostra i template disponibili nella directory `minutes/templates/`. Ogni template definisce la struttura e il tono del verbale (vedi [Installare nuovi template](#installare-nuovi-template)).

2. **Selezione del modello LLM** — La pagina recupera l'elenco dei modelli installati in Ollama (`GET http://localhost:11434/api/tags`) e propone una lista di scelta.

3. **Parametri opzionali** — È possibile aggiungere note contestuali (es. obiettivi della riunione, decisioni pregresse) che verranno iniettate nel prompt.

4. **Avvio generazione** — Il click su **Genera Minuta** invia una richiesta alla Minutes API (`POST /minutes`) che:
   - Crea un job nella coda Redis `minutes:queue`
   - Restituisce un `minutes_job_id`

5. **Monitoraggio** — Un indicatore di avanzamento mostra lo stato. Il Minutes Worker esegue in sequenza:
   - **RAG retrieval** — recupera da Qdrant i chunk più rilevanti della trascrizione usando `nomic-embed-text`
   - **Prompt assembly** — costruisce il prompt finale combinando template + contesto RAG + alias speaker + note
   - **Generazione LLM** — invia il prompt a Ollama in streaming
   - **Upload** — salva la minuta generata su MinIO nel bucket `minutes`

6. **Risultato** — Al completamento, la pagina mostra la minuta in un'area di testo con opzione di copia e download (formato `.md` o `.txt` a seconda del template).

---

## Installare nuovi template

I template definiscono la struttura del verbale generato dal LLM. Si trovano nella directory:

```
ghost-in-the-meeting/
└── minutes/
    └── templates/
        ├── standard.md
        ├── executive_summary.md
        └── (altri template)
```

### Struttura di un template

Un template è un file di testo (Markdown o plain text) con **placeholder** in formato Jinja2 che il Minutes Worker sostituisce prima di inviare il prompt a Ollama.

Esempio di template `minutes/templates/standard.md`:

```markdown
# Verbale di Riunione

**Data:** {{ date }}
**Cliente:** {{ customer }}
**Progetto:** {{ project }}
**Partecipanti:** {{ participants }}
**Durata:** {{ duration }}

---

## Contesto recuperato dalla trascrizione

{{ rag_context }}

---

## Istruzioni per il modello

Sei un assistente specializzato nella redazione di verbali aziendali.
Basandoti sul contesto della trascrizione fornito sopra, genera un verbale strutturato che includa:

1. **Sommario** — breve descrizione degli argomenti trattati
2. **Punti discussi** — elenco degli argomenti affrontati con relativi dettagli
3. **Decisioni prese** — elenco delle decisioni formali
4. **Azioni da intraprendere** — lista di task con responsabile e scadenza, se disponibile
5. **Note aggiuntive** — tutto ciò che non rientra nelle categorie precedenti

Usa un tono {{ tone }} e scrivi in {{ language }}.
```

### Placeholder disponibili

| Placeholder | Descrizione |
|---|---|
| `{{ date }}` | Data della riunione (dal metadata del job) |
| `{{ customer }}` | Nome cliente |
| `{{ project }}` | Nome progetto |
| `{{ participants }}` | Elenco partecipanti (dagli alias speaker) |
| `{{ duration }}` | Durata audio trascritta |
| `{{ rag_context }}` | Chunk di trascrizione recuperati da Qdrant (iniettato automaticamente dal worker) |
| `{{ language }}` | Lingua della trascrizione (`italiano`, `inglese`) |
| `{{ tone }}` | Tono richiesto (campo configurabile dall'utente nella UI) |
| `{{ notes }}` | Note aggiuntive inserite dall'utente nella pagina Minuta |

### Creare un nuovo template

1. Creare un nuovo file `.md` nella directory `minutes/templates/`:

```bash
cp minutes/templates/standard.md minutes/templates/mio_template.md
```

2. Modificare il contenuto adattando le istruzioni al caso d'uso (es. template per board meeting, per review tecnica, per kickoff di progetto).

3. **Non è necessario riavviare Docker**: i template sono montati come volume read-only nel container (`./minutes/templates:/app/templates:ro`). Al successivo accesso alla pagina Minuta, il nuovo template apparirà automaticamente nel menu a tendina.

4. Verificare che il file sia riconosciuto:

```bash
docker exec n8n_minutes_api ls /app/templates
```

### Regole per i template

- Il nome del file (senza estensione) diventa l'etichetta nel menu a tendina della UI, con underscore trasformati in spazi (`executive_summary` → `executive summary`)
- Il template deve contenere almeno il placeholder `{{ rag_context }}`, altrimenti il contesto RAG non verrà incluso nel prompt
- È possibile usare tutta la sintassi Jinja2 (condizioni `{% if %}`, cicli `{% for %}`, filtri come `{{ participants | join(', ') }}`)
- La lunghezza del template incide sul numero di token inviati a Ollama: template molto lunghi possono superare la context window dei modelli più piccoli

---

## Installare nuovi modelli con Ollama

Il sistema usa Ollama per due funzioni distinte:

| Funzione | Modello default | Variabile env |
|---|---|---|
| **Embedding** (RAG) | `nomic-embed-text` | `EMBED_MODEL` in `minutes_worker` / `rag_indexer` |
| **Generazione verbale** | scelto dall'utente nella UI | configurato al momento della richiesta |

### Installare un nuovo modello LLM

```bash
# Elenco modelli consigliati per diversi scenari
docker exec -it n8n_stack_ollama ollama pull mistral          # 7B, veloce su CPU, ottimo per italiano
docker exec -it n8n_stack_ollama ollama pull llama3.2         # Meta LLaMA 3.2, eccellente qualità
docker exec -it n8n_stack_ollama ollama pull gemma3:12b       # Google Gemma 3, buona comprensione contesto
docker exec -it n8n_stack_ollama ollama pull phi4             # Microsoft Phi-4, leggero e preciso
docker exec -it n8n_stack_ollama ollama pull qwen2.5:7b       # Qwen 2.5, ottimo per italiano/cinese
```

Una volta completato il download, il modello appare automaticamente nel menu a tendina della pagina Generazione Minuta (la UI interroga `GET http://localhost:11434/api/tags` a ogni apertura).

### Verificare i modelli installati

```bash
docker exec -it n8n_stack_ollama ollama list
```

### Rimuovere un modello

```bash
docker exec -it n8n_stack_ollama ollama rm nome_modello
```

### Cambiare il modello di embedding

Il modello di embedding è usato da due worker (`rag_indexer` e `minutes_worker`) e deve essere lo stesso per entrambi, pena incompatibilità tra i vettori in Qdrant.

1. Installare il nuovo modello:

```bash
docker exec -it n8n_stack_ollama ollama pull mxbai-embed-large
```

2. Aggiornare `docker-compose.yml` per entrambi i servizi:

```yaml
# rag_indexer e minutes_worker — modificare entrambi
environment:
  - EMBED_MODEL=mxbai-embed-large
  - EMBED_DIM=1024  # adattare alla dimensione del nuovo modello
```

3. **Attenzione:** cambiare il modello di embedding invalida tutti i vettori già indicizzati in Qdrant. Prima di riavviare, cancellare la collection esistente:

```bash
curl -X DELETE http://localhost:6333/collections/transcriptions
```

4. Riavviare i worker:

```bash
docker compose restart rag_indexer minutes_worker
```

### Dove vengono salvati i modelli

I modelli Ollama vengono salvati nel volume Docker `./ollama_data`, mappato a `/root/.ollama` all'interno del container. Sono persistenti tra i riavvii di Docker.

```bash
# Spazio occupato dai modelli
docker exec n8n_stack_ollama du -sh /root/.ollama
```

---

## Riferimento API

### Servizi e porte

| Servizio | URL | Credenziali |
|---|---|---|
| Web UI | http://localhost:8081 | — |
| WhisperX API | http://localhost:9200 | — |
| SSE Broker | http://localhost:9400 | — |
| Transcriber API | http://localhost:9500 | — |
| Minutes API | http://localhost:9600 | — |
| n8n | http://localhost:5678 | `N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD` |
| MinIO Console | http://localhost:9001 | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` |
| Ollama | http://localhost:11434 | — |
| Qdrant | http://localhost:6333 | — |

### Endpoint principali WhisperX API (`localhost:9200`)

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/jobs` | Invia un nuovo job di trascrizione |
| `GET` | `/jobs/{id}` | Polling stato e URL output |
| `POST` | `/jobs/{id}/pause` | Sospende il job (audio conservato) |
| `POST` | `/jobs/{id}/resume` | Riprende un job sospeso |
| `POST` | `/jobs/{id}/retry` | Riprova un job fallito |
| `DELETE` | `/jobs/{id}?scope=` | Elimina risorse (`audio`/`transcript`/`rag`/`purge`) |
| `GET` | `/queue-stats` | Snapshot della coda Redis |
| `GET` | `/health` | Health check |

---

## Comandi utili

```bash
# Log in tempo reale del worker di trascrizione
docker logs n8n_whisperx_worker --follow

# Log del worker di generazione verbale
docker logs n8n_minutes_worker --follow

# Controllare la lunghezza delle code Redis
docker exec n8n_stack_redis redis-cli LLEN wx:queue
docker exec n8n_stack_redis redis-cli LLEN minutes:queue
docker exec n8n_stack_redis redis-cli LLEN rag:queue

# Ispezionare un job su Redis
docker exec n8n_stack_redis redis-cli HGETALL wx:job:JOB_ID

# Elencare tutti i file su MinIO
docker exec n8n_stack_minio sh -c '
  mc alias set local http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD > /dev/null &&
  mc ls --recursive local/wx-transcriptions &&
  mc ls --recursive local/minutes
'

# Reset completo (Redis + PostgreSQL + MinIO + disco)
./reset.sh

# Riavviare un singolo servizio senza fermare lo stack
docker compose restart whisperx_worker

# Verificare lo spazio occupato dai dati
du -sh data/
```

---

## Performance

Misurata su Apple M3 Pro, senza GPU (Hypervisor macOS non espone GPU/Neural Engine a Docker):

| Durata audio | Trascrizione | Diarizzazione | Totale |
|---|---|---|---|
| 72 minuti | ~18 min | ~35 min | ~55 min |

- Modello Whisper: `small`, compute type: `int8`
- Docker Desktop: 12 CPU, 16 GB RAM

Per file più brevi (< 30 minuti), il tempo totale scala circa linearmente.

---

## Roadmap

- [x] Pipeline di trascrizione asincrona (faster-whisper + pyannote)
- [x] Diarizzazione parlanti con progresso in tempo reale
- [x] Doppio formato output (JSON + TXT)
- [x] Coda job con recovery da stale
- [x] Cleanup worker per file temporanei
- [x] Path MinIO leggibili (`cliente/progetto/data_id/`)
- [x] SSE broker per aggiornamenti browser in tempo reale
- [x] Web UI con stato job live e passi pipeline
- [x] Pausa / Ripresa job
- [x] Eliminazione risorse job (4 scope: audio / transcript / rag / purge)
- [x] Stato DELETED — soft delete con retention metadata
- [x] Rename speaker (alias partecipanti → `SPEAKER_00`)
- [x] Indicizzazione RAG trascrizioni su Qdrant
- [x] Generazione verbale via Ollama
- [ ] Esportazione verbale in DOCX
- [ ] Workflow n8n end-to-end con notifica email
- [ ] Supporto GPU su Linux (CUDA)
- [ ] UI di ricerca full-text sulle trascrizioni archiviate
- [ ] Push Notification da browser

---

*Licenza MIT — contributi benvenuti.*