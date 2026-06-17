# frontend/Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Multi-Stage Build per il frontend Vite/Alpine.js (air-gapped).
#
# Stage 1 "builder": Node.js compila i sorgenti in file statici ottimizzati.
# Stage 2 "production": Nginx serve solo quei file statici — Node.js non entra
#   nell'immagine finale, che risulta minuscola (~25 MB) e priva di superficie
#   d'attacco Node.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# Installa dipendenze prima del codice (sfrutta layer cache Docker)
COPY package*.json ./
RUN npm ci --prefer-offline

# Copia i sorgenti
COPY . .

# Compila il bundle di produzione.
# VITE_API_BASE_URL="" → le fetch JS usano percorsi relativi (same-origin),
# così Nginx può fare il proxy verso il backend senza CORS.
# Se in futuro vuoi un URL assoluto (es. API su dominio diverso),
# cambia questo ARG senza toccare il codice sorgente.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ── Stage 2: Production (Nginx) ───────────────────────────────────────────────
FROM nginx:1.27-alpine AS production

# Rimuovi la configurazione di default di Nginx
RUN rm /etc/nginx/conf.d/default.conf

# Copia la nostra configurazione personalizzata
COPY nginx.conf /etc/nginx/conf.d/app.conf

# Copia SOLO i file compilati dallo stage builder (Node.js rimane fuori)
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
