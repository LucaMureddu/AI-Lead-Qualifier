# RUNBOOK Operativo — AI Lead Qualifier (On-Premise)

**Stack**: Docker Compose (`docker-compose.prod.yml`) · PostgreSQL 16 + pgvector · Redis 7 · ARQ Worker · Ollama (host, GPU)  
**Aggiornato**: 2026-06-18  
**Owner**: Ops / Tech Lead

---

## Indice

1. [Backup logico di Postgres](#1-backup-logico-di-postgres)
2. [Restore logico di Postgres](#2-restore-logico-di-postgres)
3. [Flush della coda ARQ su Redis](#3-flush-della-coda-arq-su-redis)
4. [Riavvio forzato del worker — GPU OOM](#4-riavvio-forzato-del-worker--gpu-oom)
5. [Verifica stato generale dello stack](#5-verifica-stato-generale-dello-stack)

> **Convenzione**: ogni comando è **copia-incolla** e fa riferimento ai nomi container definiti in `docker-compose.prod.yml`.  
> Sostituire `$REDIS_PASSWORD` con il valore di `REDIS_PASSWORD` dal file `.env.prod` sul server.

---

## 1. Backup logico di Postgres

Il backup viene eseguito **dentro il container** `postgres` con `pg_dump`.  
Il file di dump è scritto sul **filesystem host** nella directory `/opt/backups/ai_lead_qualifier/`.

### 1.1 — Creare la directory di backup (prima esecuzione)

```bash
mkdir -p /opt/backups/ai_lead_qualifier
```

### 1.2 — Eseguire il backup (formato custom pg_dump)

```bash
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
docker exec -t postgres pg_dump \
  -U app \
  -d ai_lead_qualifier \
  --format=custom \
  --compress=9 \
  > "/opt/backups/ai_lead_qualifier/ai_lead_qualifier_${TIMESTAMP}.dump"
```

> **Formato custom** (`-Fc`): compresso, parallelizzabile in restore, più piccolo del plain SQL.  
> Verificare che il dump non sia vuoto:

```bash
ls -lh /opt/backups/ai_lead_qualifier/
```

### 1.3 — Backup automatico via cron (opzionale)

Aggiungere a `crontab -e` dell'utente `deploy`:

```cron
0 3 * * * TIMESTAMP=$(date +\%Y\%m\%d_\%H\%M\%S) && docker exec -t postgres pg_dump -U app -d ai_lead_qualifier --format=custom --compress=9 > "/opt/backups/ai_lead_qualifier/ai_lead_qualifier_${TIMESTAMP}.dump" 2>>/var/log/pg_backup.log
```

---

## 2. Restore logico di Postgres

> ⚠️ **ATTENZIONE**: il restore sovrascrive il database esistente.  
> Fermare prima il backend e il worker per evitare scritture concorrenti durante il restore.

### 2.1 — Fermare backend e worker

```bash
docker compose -f docker-compose.prod.yml stop backend worker
```

### 2.2 — Eseguire il restore

Sostituire `<DUMP_FILE>` con il percorso assoluto del file `.dump` da ripristinare:

```bash
docker exec -i postgres pg_restore \
  -U app \
  -d ai_lead_qualifier \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  < /opt/backups/ai_lead_qualifier/<DUMP_FILE>
```

Esempio concreto:

```bash
docker exec -i postgres pg_restore \
  -U app \
  -d ai_lead_qualifier \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  < /opt/backups/ai_lead_qualifier/ai_lead_qualifier_20260618_030000.dump
```

### 2.3 — Verificare il restore

```bash
docker exec -t postgres psql -U app -d ai_lead_qualifier \
  -c "SELECT COUNT(*) FROM langchain_pg_embedding;"
```

Output atteso: numero di righe nel catalogo pgvector (> 0 se il DB era popolato).

### 2.4 — Riavviare backend e worker

```bash
docker compose -f docker-compose.prod.yml start backend worker
```

### 2.5 — Verificare l'health check

```bash
docker compose -f docker-compose.prod.yml ps
curl -sf http://localhost:8000/health && echo "OK"
```

---

## 3. Flush della coda ARQ su Redis

Da eseguire quando la coda ARQ è in uno stato inconsistente (job bloccati in
`in-progress` dopo un crash del worker, job stale che non verranno mai processati).

> **Impatto**: i job in coda vengono eliminati. I client che fanno polling su
> `/status/{thread_id}` riceveranno 404 o uno stato stale fino alla prossima
> re-submission del lead. Comunicare al team prima di eseguire in produzione.

### 3.1 — Ispezionare la coda prima del flush

```bash
docker exec -t redis redis-cli -a "$REDIS_PASSWORD" \
  LLEN arq:queue
```

Elenco dei job in-progress:

```bash
docker exec -t redis redis-cli -a "$REDIS_PASSWORD" \
  KEYS 'arq:in-progress*'
```

### 3.2 — Flush selettivo: solo job in coda (non i risultati)

Elimina solo `arq:queue` (i job in attesa di esecuzione):

```bash
docker exec -t redis redis-cli -a "$REDIS_PASSWORD" \
  DEL arq:queue
```

### 3.3 — Flush completo: tutta la namespace ARQ

Elimina job in coda, job in-progress e risultati memorizzati (`arq:result:*`):

```bash
docker exec -t redis redis-cli -a "$REDIS_PASSWORD" \
  --scan --pattern 'arq:*' \
  | xargs -r \
  docker exec -i redis redis-cli -a "$REDIS_PASSWORD" DEL
```

> `--scan --pattern` è sicuro su Redis con molte chiavi (non usa KEYS bloccante).

### 3.4 — Riavviare il worker dopo il flush

```bash
docker compose -f docker-compose.prod.yml restart worker
```

### 3.5 — Verificare che il worker sia pronto

```bash
docker compose -f docker-compose.prod.yml logs --tail=20 worker
```

Output atteso (ARQ ready):

```
Starting worker for 3 functions: run_qualification_task, run_qualification_task_resume, run_ingestion_task
```

---

## 4. Riavvio forzato del worker — GPU OOM

Si verifica quando Ollama (in esecuzione **sul host**, non in Docker) esaurisce
la VRAM durante l'inferenza. Il worker crasha con errore `CUDA out of memory`
nei log.

### 4.1 — Diagnosticare il problema

Controllare i log del worker:

```bash
docker compose -f docker-compose.prod.yml logs --tail=50 worker | grep -i "oom\|cuda\|memory\|killed"
```

Controllare lo stato della GPU sul host:

```bash
nvidia-smi
```

Verificare se Ollama è ancora in esecuzione:

```bash
systemctl status ollama 2>/dev/null || pgrep -a ollama
```

### 4.2 — Liberare la VRAM: fermare il worker Docker

```bash
docker compose -f docker-compose.prod.yml stop worker
```

### 4.3 — Scaricare il modello da VRAM (senza killare Ollama)

```bash
# Forza Ollama a scaricare i modelli dalla VRAM.
# Il keep_alive=0 su una request dummy scarica il contesto in memoria.
curl -s http://localhost:11434/api/generate \
  -d '{"model":"llama3","keep_alive":0,"prompt":""}' \
  > /dev/null
```

Verificare che la VRAM sia liberata:

```bash
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
```

### 4.4 — Se Ollama è in crash (processo non risponde)

```bash
# Fermare il servizio Ollama.
systemctl stop ollama 2>/dev/null || pkill -f "ollama serve"

# Attendere che la VRAM sia completamente liberata.
sleep 5

# Riavviare Ollama.
systemctl start ollama 2>/dev/null || (ollama serve &)

# Attendere che il server sia pronto (health check).
sleep 10
curl -sf http://localhost:11434/api/tags && echo "Ollama OK"
```

### 4.5 — Riavviare il container worker

```bash
docker compose -f docker-compose.prod.yml start worker
```

### 4.6 — Verificare il riavvio

```bash
docker compose -f docker-compose.prod.yml logs --tail=30 worker
```

Output atteso dopo il riavvio corretto:

```
Starting worker for 3 functions: run_qualification_task, run_qualification_task_resume, run_ingestion_task
```

### 4.7 — Prevenzione: limite concorrenza worker

La variabile `ARQ_MAX_JOBS` in `.env.prod` controlla la concorrenza del worker.
Con GPU condivisa mantenerla a `1`:

```bash
grep ARQ_MAX_JOBS /opt/ai-lead-qualifier/.env.prod
# output atteso:
# ARQ_MAX_JOBS=1
```

Se il problema OOM si ripete aumentare `ARQ_JOB_TIMEOUT` e verificare la
lunghezza dei prompt (catalogo molto grande → chunk più piccoli in `INGESTION_CHUNK_SIZE`).

---

## 5. Verifica stato generale dello stack

Da eseguire dopo ogni intervento per confermare che tutti i servizi siano `healthy`.

```bash
docker compose -f docker-compose.prod.yml ps
```

Output atteso (tutti `healthy` o `running`):

```
NAME        STATUS          PORTS
backend     running (healthy)
frontend    running
postgres    running (healthy)
redis       running (healthy)
traefik     running
worker      running
```

Health check del backend:

```bash
curl -sf http://localhost:8000/health && echo "backend OK"
```

Verifica connessione Postgres dall'interno del container:

```bash
docker exec -t postgres pg_isready -U app -d ai_lead_qualifier
```

Verifica Redis:

```bash
docker exec -t redis redis-cli -a "$REDIS_PASSWORD" ping
# output atteso: PONG
```

Verifica Ollama sul host:

```bash
curl -sf http://localhost:11434/api/tags | python3 -c "import sys,json; models=[m['name'] for m in json.load(sys.stdin).get('models',[])]; print('Ollama OK — modelli:', models)"
```
