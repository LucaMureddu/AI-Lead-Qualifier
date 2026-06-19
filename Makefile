# Makefile — scorciatoie identiche tra locale e CI.
# NB: le righe dei comandi sono indentate con TAB (requisito di make).
# Tutti i comandi Python girano da backend/ (dove vivono sorgenti e pyproject.toml).

.PHONY: install install-ci test test-fast test-integration cov eval-local eval-live eval-snapshot lint check ci db-migrate db-seed secrets-check

# installa le dipendenze di sviluppo (locale: include i backend LLM)
install:
	cd backend && pip install -r requirements-dev.txt

# installa le dipendenze minime per i test/CI (senza llama-cpp-python/groq)
install-ci:
	cd backend && pip install -r requirements-ci.txt

# ── Test ──────────────────────────────────────────────────────────────────────

# test veloci (no eval live, no vector_store): unit + Binario A (eval_ci)
# Specchio esatto del job `test-fast` in ci.yml — senza Docker.
test-fast:
	cd backend && pytest -m "not eval and not vector_store" --cov --cov-report=term-missing -q

# test di integrazione pgvector (richiede Docker con immagine pgvector/pgvector:pg16)
# Specchio esatto del job `test-integration` in ci.yml.
test-integration:
	TESTCONTAINERS_RYUK_DISABLED=true \
	cd backend && pytest -m "vector_store" -v --tb=short

# alias retrocompatibile: gira test veloci (come prima del V2)
test:
	cd backend && pytest -m "not eval and not vector_store"

# test con report di copertura (solo unit, no container)
cov:
	cd backend && pytest -m "not eval and not vector_store" --cov --cov-report=term-missing

# Binario B: evals LIVE col modello reale + giudice LLM (richiede Ollama)
eval-local:
	cd backend && pytest -m eval

# Evals LLM-as-a-judge sul motore pgvector V2 (richiede Ollama + Postgres/pgvector con catalogo ingestito)
# Non gira in CI — escluso di default da -m "not eval_live".
# Vedi: backend/tests/evals/test_llm_judge.py
eval-live:
	cd backend && pytest -m eval_live -v --tb=short

# rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
eval-snapshot:
	cd backend && python -m tests.evals.capture_snapshots

# ── Lint / Type-check ─────────────────────────────────────────────────────────

# lint + type-check (gira da backend/ per trovare pyproject.toml)
# Copre gli stessi moduli del job lint-and-type in ci.yml.
lint:
	cd backend && ruff check . && mypy core agents ingestion adapters api database services worker

# ── Gate locale (tutto ciò che gira su ogni PR) ───────────────────────────────

# Replica l'intera CI in locale (tranne Playwright E2E e il deploy SSH):
#   1. lint + type-check
#   2. test veloci con copertura
#   3. test di integrazione pgvector (richiede Docker)
ci: lint test-fast test-integration
	@echo ""
	@echo "✓ CI locale completata. Se verde, la PR è pronta per review."

# alias retrocompatibile
check: lint test-fast

# ── Migrazioni DB ─────────────────────────────────────────────────────────────

# Applica tutte le migrazioni Alembic al DB indicato da DATABASE_DSN.
# Esempio: DATABASE_DSN=postgresql://app:pass@localhost/mydb make db-migrate
db-migrate:
	cd backend && alembic upgrade head

# Popola il DB con dati seed (richiede db-migrate eseguito in precedenza).
db-seed:
	cd backend && python seed_db.py

# ── Checklist segreti GitHub (prima del primo deploy) ────────────────────────
#
# Configurare in: repository → Settings → Secrets and variables → Actions
#
# Segreti INFRASTRUTTURA (deploy SSH — deploy.yml):
#   SERVER_HOST          IP o hostname del server on-premise
#   SERVER_USER          utente SSH (es. deploy, ubuntu)
#   SERVER_SSH_KEY       chiave privata SSH completa (incluso header/footer)
#   SERVER_PORT          porta SSH (default: 22)
#   SERVER_DEPLOY_PATH   path assoluto del progetto sul server
#                        (es. /opt/ai-lead-qualifier)
#
# NON aggiungere come segreti GitHub (vivono in .env.prod sul server):
#   DATABASE_DSN, REDIS_DSN, POSTGRES_PASSWORD, REDIS_PASSWORD,
#   JWT_PRIVATE_KEY_PATH, ACME_EMAIL, APP_DOMAIN, API_DOMAIN
#   → questi non transitano MAI per GitHub Actions.
#
# Verifica che i segreti siano configurati:
secrets-check:
	@echo "Segreti richiesti da deploy.yml:"
	@echo "  SERVER_HOST          → $(if $(SERVER_HOST),✓ impostato in env,✗ mancante)"
	@echo "  SERVER_USER          → $(if $(SERVER_USER),✓ impostato in env,✗ mancante)"
	@echo "  SERVER_SSH_KEY       → (non verificabile in locale — controllare GitHub UI)"
	@echo "  SERVER_PORT          → $(if $(SERVER_PORT),✓ impostato in env,✗ mancante [default: 22])"
	@echo "  SERVER_DEPLOY_PATH   → $(if $(SERVER_DEPLOY_PATH),✓ impostato in env,✗ mancante)"
	@echo ""
	@echo "Verificare anche in GitHub: Settings → Secrets → Actions"
