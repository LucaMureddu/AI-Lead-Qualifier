#!/usr/bin/env python3
"""
tests/evals/capture_snapshots.py
--------------------------------
Rigenera ``snapshots/extractions.json`` eseguendo la pipeline REALE (sanitizer →
extractor con LLM vero) su ogni lead del golden dataset.

È lo script da lanciare A MANO sul Mac (con Ollama attivo) quando si modifica il
prompt, il modello o il golden dataset — così il Binario A (eval_ci, model-free)
resta allineato alle generazioni più recenti.

Uso:
    # dalla root del progetto, con il .venv attivo e Ollama in esecuzione
    python -m tests.evals.capture_snapshots

Oppure (eseguibile):
    ./tests/evals/capture_snapshots.py

Variabili d'ambiente rilevanti (vedi core/config.py): LLM_PROVIDER, LLM_BASE_URL,
LLM_MODEL_NAME. Di default punta all'endpoint OpenAI-compatibile locale (Ollama).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from agents.extractor import extractor_node
from agents.sanitizer import sanitizer_node
from core.config import get_settings
from core.state import LeadContext

_HERE = Path(__file__).resolve().parent
_GOLDEN_PATH = _HERE / "datasets" / "leads_golden.jsonl"
_SNAPSHOT_PATH = _HERE / "snapshots" / "extractions.json"
_NORM_PATH = _HERE / "snapshots" / "normalizations.json"
_ROOT = _HERE.parents[1]
_CATALOGS = ["dirty_catalog.csv", "catalogo_problematico.csv"]


def _load_golden() -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in _GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _extract(raw_text: str, lead_id: str) -> List[str]:
    """Esegue sanitizer + extractor REALI e ritorna i servizi estratti."""
    state: Dict[str, Any] = {
        "lead": LeadContext(lead_id=lead_id, tenant_id="eval", raw_payload={"text": raw_text}),
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
    state.update(sanitizer_node(state))
    out = await extractor_node(state)
    return out.get("extracted_services", [])


async def main() -> None:
    settings = get_settings()
    rows = _load_golden()
    print(
        f"[capture] {len(rows)} lead | provider={settings.llm_provider} "
        f"| model={settings.llm_model_name} | base={settings.llm_base_url}"
    )

    extractions: List[Dict[str, Any]] = []
    for row in rows:
        services = await _extract(row["raw_text"], row["id"])
        print(f"  {row['id']}: {services}")
        extractions.append({"id": row["id"], "output": services})

    payload = {
        "_comment": "Generato da tests/evals/capture_snapshots.py — NON modificare a mano.",
        "_generated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.llm_model_name,
        "extractions": extractions,
    }
    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[capture] scritto {_SNAPSHOT_PATH}")

    # ── Normalizzazioni (cataloghi sporco/problematico) → Binario A normalizzazione ──
    from tests.evals._pipeline import run_normalization  # noqa: PLC0415

    catalogs: Dict[str, Any] = {}
    for name in _CATALOGS:
        res = await run_normalization(_ROOT / name)
        print(
            f"  [norm] {name}: items={len(res['items'])} "
            f"flagged={res['flagged_count']} conf={res['confidence_score']:.2f}"
        )
        catalogs[name] = {
            "flagged_count": res["flagged_count"],
            "confidence_score": res["confidence_score"],
            "items": res["items"],
        }
    norm_payload = {
        "_comment": "Generato da tests/evals/capture_snapshots.py — NON modificare a mano.",
        "_generated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.llm_model_name,
        "catalogs": catalogs,
    }
    _NORM_PATH.write_text(json.dumps(norm_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[capture] scritto {_NORM_PATH}")

    # ── Mapping (RAG su ChromaDB) → Binario A mapping. Richiede ChromaDB attivo. ──
    # Graceful: se Chroma è giù, salta senza interrompere extraction/normalization.
    try:
        from tests.evals._mapper import run_mapper, seed_catalog  # noqa: PLC0415

        seed_catalog(settings.chroma_host, settings.chroma_port)
        map_golden = [
            json.loads(line)
            for line in (_HERE / "datasets" / "mappings_golden.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        mappings = []
        for row in map_golden:
            mapped = await run_mapper([row["query"]])
            best = mapped[0] if mapped else {}
            print(f"  [map] {row['query']} → {best.get('matched_name', '∅')} (d={best.get('distance')})")
            mappings.append({
                "query": row["query"],
                "matched_name": best.get("matched_name", ""),
                "distance": best.get("distance"),
            })
        map_payload = {
            "_comment": "Generato da tests/evals/capture_snapshots.py — NON modificare a mano.",
            "_generated": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": "all-MiniLM-L6-v2 (ChromaDB)",
            "mappings": mappings,
        }
        _MAP_PATH = _HERE / "snapshots" / "mappings.json"
        _MAP_PATH.write_text(json.dumps(map_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[capture] scritto {_MAP_PATH}")
    except Exception as exc:  # noqa: BLE001
        print(f"[capture] mapping SALTATO (ChromaDB non raggiungibile?): {exc}")


if __name__ == "__main__":
    asyncio.run(main())
