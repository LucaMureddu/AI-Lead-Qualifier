"""
tests/evals/test_extraction_eval.py
-----------------------------------
BINARIO B — evals LIVE col modello reale (marker ``eval``, ESCLUSO da
``-m "not eval"``). Si lancia SOLO in locale, con Ollama attivo:

    pytest -m eval

Due livelli di valutazione:
1. ``test_extraction_live`` (parametrizzato, deterministico) — invoca la pipeline
   reale (sanitizer → extractor) e verifica numero servizi + keyword multilingua
   (robusto all'output EN/IT) o similarità semantica.
2. ``test_extraction_judged_passrate`` (aggregato) — GIUDICE LLM locale: chiede a
   un modello se l'estrazione è pertinente all'intento PRINCIPALE del lead.
   Essendo un giudizio sfumato, NON è tutto-o-niente: si richiede un pass-rate
   minimo (§4.5), così un singolo falso negativo del giudice non rompe la suite.

Tutte le chiamate LLM sono DENTRO le funzioni di test (mai a import-time), così
il file resta importabile/deselezionabile anche senza Ollama.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

import pytest

from agents.extractor import _call_openai_compatible, extractor_node
from agents.sanitizer import sanitizer_node
from core.state import LeadContext
from tests.evals.semantic import semantic_score

pytestmark = pytest.mark.eval

_HERE = pathlib.Path(__file__).resolve().parent
_SEMANTIC_THRESHOLD = 0.5
_JUDGE_PASS_RATE = 0.75   # tollera 1 miss su 4; alzare man mano che il golden cresce (§4.5)

_GOLDEN: List[Dict[str, Any]] = [
    json.loads(line)
    for line in (_HERE / "datasets" / "leads_golden.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
_NON_FALLBACK = [r for r in _GOLDEN if not (r.get("expect_human_fallback") or r.get("expect_services") == 0)]


# ── Pipeline reale (sanitizer → extractor) ─────────────────────────────────────

async def _run_extractor(raw_text: str) -> List[str]:
    """Esegue sanitizer + extractor REALI (LLM vero) e ritorna i servizi estratti."""
    state: Dict[str, Any] = {
        "lead": LeadContext(lead_id="eval-lead", tenant_id="eval", raw_payload={"text": raw_text}),
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "queued",
        "error_detail": None,
        "sanitized_text": "",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "retry_count": 0,
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }
    state.update(sanitizer_node(state))          # maschera PII prima dell'LLM
    out = await extractor_node(state)            # chiama il VERO Ollama
    return out.get("extracted_services", [])


def _category_matched(keywords: List[str], services: List[str]) -> bool:
    low = [s.lower() for s in services]
    if any(kw.lower() in s for s in low for kw in keywords):
        return True
    return max((semantic_score(kw, services) for kw in keywords), default=0.0) >= _SEMANTIC_THRESHOLD


@pytest.mark.parametrize("row", _GOLDEN, ids=[r["id"] for r in _GOLDEN])
async def test_extraction_live(row: Dict[str, Any]) -> None:
    services = await _run_extractor(row["raw_text"])

    if row.get("expect_human_fallback") or row.get("expect_services") == 0:
        assert services == [], f"{row['id']}: atteso nessun servizio, ottenuto {services}"
        return

    assert len(services) >= row["min_services"], (
        f"{row['id']}: attesi >= {row['min_services']} servizi, ottenuti {services}"
    )
    assert _category_matched(row["expect_keywords"], services), (
        f"{row['id']}: nessuna keyword {row['expect_keywords']} nei servizi {services}"
    )


# ── Giudice LLM locale (rubric su intento principale) ──────────────────────────

_JUDGE_SYSTEM = (
    "Sei un valutatore di un sistema di estrazione servizi B2B. Giudica se i servizi "
    "estratti sono RAGIONEVOLMENTE PERTINENTI all'intento PRINCIPALE del lead. "
    "Non è richiesto che coprano ogni dettaglio secondario: se l'estrazione coglie il "
    "bisogno centrale, il verdetto è PASS. Rispondi ESCLUSIVAMENTE con un oggetto JSON "
    'valido {"verdict": "PASS" | "FAIL", "reason": "<breve motivazione>"} '
    "senza testo aggiuntivo e senza markdown."
)


def _parse_judge_verdict(raw: str) -> Dict[str, str]:
    """Estrae {verdict, reason} dalla risposta del giudice (robusto a rumore)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
    verdict = str(data.get("verdict", "FAIL")).upper()
    return {"verdict": "PASS" if verdict == "PASS" else "FAIL", "reason": str(data.get("reason", ""))}


async def _llm_judge(raw_text: str, services: List[str]) -> Dict[str, str]:
    """Chiede al modello locale di giudicare la pertinenza dell'estrazione."""
    user = (
        f"Richiesta del lead:\n{raw_text}\n\n"
        f"Servizi estratti dal sistema:\n{json.dumps(services, ensure_ascii=False)}\n\n"
        "I servizi estratti sono pertinenti all'intento principale del lead? "
        'Rispondi col JSON {"verdict": ..., "reason": ...}.'
    )
    raw = await _call_openai_compatible(_JUDGE_SYSTEM, user)
    return _parse_judge_verdict(raw)


async def test_extraction_judged_passrate() -> None:
    """Pass-rate del giudice LLM sui casi non-fallback (tollerante, vedi §4.5)."""
    results: List[tuple] = []
    for row in _NON_FALLBACK:
        services = await _run_extractor(row["raw_text"])
        verdict = await _llm_judge(row["raw_text"], services)
        results.append((row["id"], verdict["verdict"] == "PASS", services, verdict["reason"]))

    passed = sum(1 for _, ok, _, _ in results if ok)
    rate = passed / len(results) if results else 0.0
    report = "\n".join(
        f"  {rid}: {'PASS' if ok else 'FAIL'} {svc} — {reason}"
        for rid, ok, svc, reason in results
    )
    print(f"\n[giudice LLM] pass-rate {passed}/{len(results)} = {rate:.0%}\n{report}")

    assert rate >= _JUDGE_PASS_RATE, (
        f"pass-rate giudice {rate:.0%} < soglia {_JUDGE_PASS_RATE:.0%}\n{report}"
    )
