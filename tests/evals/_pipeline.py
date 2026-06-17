"""
tests/evals/_pipeline.py
------------------------
Helper condiviso (Binario B + capture_snapshots) per eseguire la pipeline di
ingestion fino alla validazione, SENZA scrivere su ChromaDB:

    chunker (legge il CSV) → normalizer (LLM, loop sui chunk) → validator

Si ferma prima del finalizer/approval: ci interessano gli item normalizzati e
gli esiti di flagging (per gli eval di normalizzazione e di routing HITL).
Usa il VERO LLM (normalizer) → da chiamare solo in contesti `-m eval` o dallo
script di cattura snapshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ingestion.graph import chunker_node, make_initial_state, normalizer_node, validator_node


def _serialize(items) -> List[Dict[str, Any]]:
    return [
        {"name": it.name, "price": it.price, "currency": it.currency, "flagged": it.flagged}
        for it in items
    ]


async def run_normalization(
    csv_path: Path,
    tenant_id: str = "eval",
    review_feedback: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Esegue chunker → normalizer (loop) → validator su un CSV e ritorna:
        { items: [{name, price, currency, flagged}], flagged_count,
          confidence_score, validation_errors, error }
    """
    state: Dict[str, Any] = dict(
        make_initial_state(
            tenant_id=tenant_id,
            source_file=str(csv_path),
            file_format="csv",
            review_feedback=review_feedback,
        )
    )

    state.update(await chunker_node(state))
    if state.get("error"):
        return {"items": [], "flagged_count": 0, "confidence_score": 0.0,
                "validation_errors": [], "error": state["error"]}

    # Loop sui chunk: accumulo manuale (fuori dal grafo non c'è il reducer operator.add).
    state.setdefault("normalized_items", [])
    while state["current_chunk_index"] < len(state["raw_chunks"]):
        patch = await normalizer_node(state)
        state["normalized_items"] = state["normalized_items"] + patch.get("normalized_items", [])
        state["current_chunk_index"] = patch["current_chunk_index"]
        if patch.get("error"):
            state["error"] = patch["error"]

    patch = await validator_node(state)
    flagged = patch.get("flagged_items", [])

    return {
        "items": _serialize(state["normalized_items"]),
        "flagged_count": len(flagged),
        "confidence_score": patch.get("confidence_score", 0.0),
        "validation_errors": patch.get("validation_errors", []),
        "error": state.get("error"),
    }
