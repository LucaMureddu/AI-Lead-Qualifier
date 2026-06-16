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
from core.state import LeadInfo

_HERE = Path(__file__).resolve().parent
_GOLDEN_PATH = _HERE / "datasets" / "leads_golden.jsonl"
_SNAPSHOT_PATH = _HERE / "snapshots" / "extractions.json"


def _load_golden() -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in _GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _extract(raw_text: str, lead_id: str) -> List[str]:
    """Esegue sanitizer + extractor REALI e ritorna i servizi estratti."""
    state: Dict[str, Any] = {
        "lead_info": LeadInfo(id=lead_id, raw_text=raw_text, tenant_id="eval"),
        "sanitized_text": "",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "retry_count": 0,
        "sse_logs": [],
        "error": None,
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


if __name__ == "__main__":
    asyncio.run(main())
