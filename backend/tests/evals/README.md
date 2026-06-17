# Evals — Livello 2 (i "due binari")

Valutazione della **qualità AI** (non solo "non crasha"), su quattro capacità:
**Extractor**, **Normalizer**, **Routing HITL**, **Mapper (RAG)**.
Vedi `TESTING_PLAN.md` §4.

## I due binari

| Binario | Marker | Dove gira | Cosa fa |
|---|---|---|---|
| **A — model-free** | `eval_ci` | **CI** (incluso in `pytest -m "not eval"`) | Legge le *generazioni registrate* negli `snapshots/` e verifica asserzioni deterministiche + semantiche. **Nessun LLM, nessun Chroma.** |
| **B — live** | `eval` | **solo locale** (`pytest -m eval`) | Esegue la pipeline reale (Ollama; Chroma per il mapper) + **giudice LLM** (extractor). Intercetta la deriva di modello/prompt. |

`eval_ci` **non** è escluso da `-m "not eval"` → il Binario A fa già parte del job `backend-tests` in CI. Il Binario B (`eval`) è escluso di default.

## Comandi (Makefile)

```bash
make test            # = pytest -m "not eval"  → unit + integration + Binario A
make eval-local      # = pytest -m eval        → Binario B (richiede Ollama; Chroma per il mapper)
make eval-snapshot   # = python -m tests.evals.capture_snapshots → rigenera gli snapshot
```

## Workflow di disciplina (§4.6)

Quando cambi **prompt, modello o catalogo**:

1. `make eval-snapshot` — rigenera `snapshots/` col modello reale (richiede Ollama; il mapping richiede anche Chroma — se Chroma è giù, il mapping viene saltato).
2. `make eval-local` — esegui il Binario B per intercettare regressioni.
3. Committa gli snapshot aggiornati: così il Binario A in CI resta allineato.

> La CI **non** esegue LLM: la deriva di modello/prompt la cattura il Binario B in locale, da lanciare **prima di mergiare** modifiche sensibili.

## File

```
datasets/      leads_golden.jsonl · mappings_golden.jsonl    (+ i CSV in root: dirty_catalog, catalogo_problematico)
snapshots/     extractions.json · normalizations.json · mappings.json   (generazioni registrate → Binario A)
semantic.py    matcher semantico PLACEHOLDER (sostituibile con all-MiniLM-L6-v2)
_pipeline.py   helper ingestion (chunker→normalizer→validator) per gli eval di normalizzazione
_mapper.py     helper seed/query Chroma per gli eval del mapper
capture_snapshots.py   rigenera TUTTI gli snapshot (extraction + normalization + mapping)

test_extraction_ci.py / _eval.py        Extractor   (A / B, + giudice LLM con pass-rate)
test_normalization_ci.py / _eval.py     Normalizer + Routing HITL (A / B)
test_mapping_ci.py / _eval.py           Mapper RAG  (A / B, skip se Chroma down)
```

## Soglie calibrabili

- Giudice LLM (extractor): `_JUDGE_PASS_RATE` in `test_extraction_eval.py`.
- Match categoria (extractor): `expect_keywords` nel golden; `_SEMANTIC_THRESHOLD`.
- Distanza mapper: `max_distance` per riga in `mappings_golden.jsonl`.

Se rigenerando uno snapshot col modello reale un'asserzione "salta", spesso è un **finding reale** (es. output in inglese, sinonimi): si calibrano le attese, non si forza il test.
