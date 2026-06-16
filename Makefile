# Makefile — scorciatoie identiche tra locale e CI (vedi TESTING_PLAN.md §9).
# NB: le righe dei comandi sono indentate con TAB (requisito di make).

.PHONY: install install-ci test cov eval-local eval-snapshot lint check

# installa le dipendenze di sviluppo (locale: include i backend LLM)
install:
	pip install -r requirements-dev.txt

# installa le dipendenze minime per i test/CI (senza llama-cpp-python/groq)
install-ci:
	pip install -r requirements-ci.txt

# test veloci (no eval live): unit + integration + Binario A (eval_ci)
test:
	pytest -m "not eval"

# test con report di copertura
cov:
	pytest -m "not eval" --cov --cov-report=term-missing

# Binario B: evals LIVE col modello reale + giudice LLM (richiede Ollama)
eval-local:
	pytest -m eval

# rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
eval-snapshot:
	python -m tests.evals.capture_snapshots

# lint + type-check
lint:
	ruff check . && mypy core agents ingestion adapters api

# tutto ciò che gira a ogni PR (gate locale)
check: lint test
