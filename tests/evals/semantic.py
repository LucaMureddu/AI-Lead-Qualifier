"""
tests/evals/semantic.py
-----------------------
Matcher semantico PLACEHOLDER per il Binario A (eval_ci), model-free e
deterministico — nessuna dipendenza, nessun modello, esecuzione istantanea.

⚠️  È un segnaposto. Quando si vorrà il coseno "vero" (TESTING_PLAN.md §4.3.2),
sostituire ``embed()`` con l'embedder ``all-MiniLM-L6-v2`` già usato da ChromaDB
(piccolo, CPU-only, deterministico): l'interfaccia (``semantic_score``) resta
identica, quindi i test del Binario A non cambiano.

Strategia attuale (finta ma sensata):
- match per *contenimento* (la categoria attesa compare nel nome del servizio) → 1.0;
- altrimenti coseno su vettori bag-of-words (token minuscoli).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List

# Flag esplicito: True finché usiamo l'embedder finto. Diventa False quando si
# innesta all-MiniLM-L6-v2.
FAKE_EMBEDDER: bool = True


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^0-9a-zàèéìòù]+", text.lower()) if t]


def embed(text: str) -> Dict[str, float]:
    """Embedding FINTO: vettore bag-of-words (frequenza dei token). Placeholder."""
    return dict(Counter(_tokens(text)))


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return num / (na * nb) if na and nb else 0.0


def semantic_score(expected: str, candidates: List[str]) -> float:
    """
    Punteggio [0,1]: quanto la categoria ``expected`` è rappresentata fra i
    ``candidates`` (i servizi estratti). Massimo su tutti i candidati.
    """
    exp = expected.lower().strip()
    if not exp or not candidates:
        return 0.0

    exp_vec = embed(exp)
    best = 0.0
    for cand in candidates:
        cl = cand.lower()
        if exp in cl or cl in exp:          # contenimento → match pieno
            return 1.0
        best = max(best, cosine(exp_vec, embed(cand)))
    return best
