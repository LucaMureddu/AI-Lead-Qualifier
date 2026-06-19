# backend/Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Immagine di produzione per il backend FastAPI (air-gapped).
# Usa python:3.11-slim per minimizzare la superficie d'attacco.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Evita che Python scriva file .pyc e bufferizzi stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dipendenze di sistema minime (build-essential serve per alcuni pacchetti C)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Layer di dipendenze Python (sfrutta la cache Docker) ─────────────────────
# Copia SOLO requirements.txt prima del codice sorgente: se non cambia,
# Docker riusa il layer già buildato senza reinstallare tutto.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Codice sorgente ───────────────────────────────────────────────────────────
COPY . .

# ── Cartelle per i volumi persistenti ────────────────────────────────────────
# Queste directory vengono montate come volumi in docker-compose.yml.
# Senza il VOLUME dichiarato qui, ogni restart del container cancellerebbe
# il database SQLite e tutti i CSV caricati.
RUN mkdir -p /app/data /app/uploads

VOLUME ["/app/data", "/app/uploads"]

# ── Porta ────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Avvio ─────────────────────────────────────────────────────────────────────
# In produzione: uvicorn diretto. Per scalabilità futura sostituire con:
# gunicorn -k uvicorn.workers.UvicornWorker main:app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
