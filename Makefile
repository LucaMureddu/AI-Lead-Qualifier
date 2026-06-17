# Makefile — scorciatoie identiche tra locale e CI.
# NB: le righe dei comandi sono indentate con TAB (requisito di make).
# Tutti i comandi Python girano da backend/ (dove vivono sorgenti e pyproject.toml).

.PHONY: install install-ci test cov eval-local eval-snapshot lint check

# installa le dipendenze di sviluppo (locale: include i backend LLM)
install:
	cd backend && pip install -r requirements-dev.txt

# installa le dipendenze minime per i test/CI (senza llama-cpp-python/groq)
install-ci:
	cd backend && pip install -r requirements-ci.txt

# test veloci (no eval live): unit + integration + Binario A (eval_ci)
test:
	cd backend && pytest -m "not eval"

# test con report di copertura
cov:
	cd backend && pytest -m "not eval" --cov --cov-report=term-missing

# Binario B: evals LIVE col modello reale + giudice LLM (richiede Ollama)
eval-local:
	cd backend && pytest -m eval

# rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
eval-snapshot:
	cd backend && python -m tests.evals.capture_snapshots

# lint + type-check (gira da backend/ per trovare pyproject.toml)
lint:
	cd backend && ruff check . && mypy core agents ingestion adapters api

# tutto ciò che gira a ogni PR (gate locale)
check: lint test
