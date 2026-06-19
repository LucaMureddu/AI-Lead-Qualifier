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
# V2.1: /app/uploads rimosso — i file catalogo sono ora su S3 (MinIO/AWS).
# Il container è completamente stateless: nessun dato persistente su disco.
# Solo /app/data è mantenuto per compatibilità con eventuali script one-shot.
RUN mkdir -p /app/data

VOLUME ["/app/data"]

# ── Porta ────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Avvio ─────────────────────────────────────────────────────────────────────
# In produzione: uvicorn diretto. Per scalabilità futura sostituire con:
# gunicorn -k uvicorn.workers.UvicornWorker main:app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
