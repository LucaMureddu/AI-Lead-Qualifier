"""
tests/evals/test_llm_judge.py
-----------------------------
LLM-as-a-judge eval suite — motore pgvector V2.

Marker : ``eval_live`` — ESCLUSO dalla CI standard e da ``-m eval``.
         Si esegue SOLO su richiesta manuale con lo stack completo attivo:

    make eval-live
    # oppure direttamente:
    pytest -m eval_live -v --tb=short

Prerequisiti di runtime
-----------------------
* Ollama in ascolto su LLM_BASE_URL con il modello configurato in LLM_MODEL_NAME
* Postgres/pgvector raggiungibile via DATABASE_DSN con il catalogo già ingestito
  (``make db-seed`` oppure caricamento file via /ingest)
* Variabili d'ambiente caricate da .env (o esportate manualmente)

Cosa valuta
-----------
Per ogni lead nel ``GOLDEN_DATASET`` viene eseguito l'INTERO grafo LangGraph V2:
  sanitizer → extractor → mapper → evaluator → calculator → delivery

Il giudice LLM valuta poi due metriche sullo stato finale:

* **recall_score** [0.0-1.0] — l'estrazione ha catturato tutti i bisogni espressi?
* **precision_score** [0.0-1.0] — il mapping pgvector ha associato voci di catalogo corrette?

Sicurezza PII
-------------
Il giudice LLM riceve SOLO ``state["sanitized_text"]``: il testo mascherato prodotto
da SanitizerNode (identico a quello che entra in produzione). Il ``raw_text`` del lead
NON raggiunge mai il modello giudice.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from core.config import get_settings
from core.graph import build_graph
from core.state import AgentState, LeadContext

pytestmark = pytest.mark.eval_live

# ── Thresholds globali ─────────────────────────────────────────────────────────

GLOBAL_PASS_RATE: float = 0.70
"""Percentuale minima di lead che devono superare ENTRAMBE le soglie individuali."""

# ── Golden Dataset ─────────────────────────────────────────────────────────────


@dataclass
class GoldenLead:
    """Caso di test del Golden Dataset per il giudice LLM."""

    lead_id: str
    tenant_id: str
    raw_text: str
    expected_services: list[str]
    """Servizi attesi — usati SOLO come contesto nel prompt del giudice, mai per assertion dirette."""
    min_recall_score: float = 0.65
    min_precision_score: float = 0.55


GOLDEN_DATASET: list[GoldenLead] = [
    GoldenLead(
        lead_id="G-001",
        tenant_id="eval-tenant",
        raw_text=(
            "Buongiorno, siamo un'azienda di medie dimensioni nel settore manifatturiero. "
            "Vorremmo realizzare un nuovo sito web aziendale moderno e responsive "
            "con integrazione al nostro CRM interno. "
            "Siamo anche interessati a una consulenza SEO per migliorare "
            "il posizionamento organico sui motori di ricerca."
        ),
        expected_services=["Sviluppo Web", "Integrazione CRM", "SEO"],
        min_recall_score=0.70,
        min_precision_score=0.60,
    ),
    GoldenLead(
        lead_id="G-002",
        tenant_id="eval-tenant",
        # Contiene PII intenzionale — SanitizerNode la maschera prima che il giudice la veda.
        raw_text=(
            "Sono il CTO di StartupX (cto@startupx.io, +39 02 9988 7766, "
            "CF RSSMRA85T10A562S). Dobbiamo migrare i nostri server fisici "
            "su infrastruttura cloud AWS. Serve un piano di backup automatizzato "
            "e una strategia di disaster recovery con RTO < 4 ore."
        ),
        expected_services=["Migrazione Cloud", "Backup", "Disaster Recovery"],
        min_recall_score=0.65,
        min_precision_score=0.55,
    ),
    GoldenLead(
        lead_id="G-003",
        tenant_id="eval-tenant",
        raw_text=(
            "We need a complete e-commerce platform for our fashion brand. "
            "Requirements include payment gateway integration (Stripe and PayPal), "
            "inventory management, and an ongoing technical maintenance plan."
        ),
        expected_services=["E-commerce", "Payment Gateway", "Inventory Management", "Maintenance"],
        min_recall_score=0.65,
        min_precision_score=0.55,
    ),
    GoldenLead(
        lead_id="G-004",
        tenant_id="eval-tenant",
        raw_text=(
            "Cerchiamo un'agenzia specializzata per un audit di sicurezza informatica "
            "della nostra rete aziendale e dei sistemi interni. "
            "Vogliamo anche un corso di formazione sulla cybersecurity per i nostri dipendenti."
        ),
        expected_services=["Security Audit", "Cybersecurity Training"],
        min_recall_score=0.70,
        min_precision_score=0.55,
    ),
    GoldenLead(
        lead_id="G-005",
        tenant_id="eval-tenant",
        raw_text=(
            "La nostra startup ha bisogno di sviluppare un'app mobile iOS e Android "
            "per la gestione degli ordini dei clienti, con notifiche push real-time "
            "e integrazione con il nostro sistema di fatturazione esistente."
        ),
        expected_services=["App Mobile iOS", "App Mobile Android", "Notifiche Push", "Fatturazione"],
        min_recall_score=0.65,
        min_precision_score=0.50,
    ),
]

# ── Graph runner ───────────────────────────────────────────────────────────────


def _build_initial_state(lead: GoldenLead) -> AgentState:
    """Costruisce lo stato iniziale V2 per il grafo LangGraph."""
    return {  # type: ignore[return-value]
        "lead": LeadContext(
            lead_id=lead.lead_id,
            tenant_id=lead.tenant_id,
            raw_payload={"text": lead.raw_text},
            metadata={},
        ),
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "processing",
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


async def _run_full_graph(lead: GoldenLead) -> AgentState:
    """
    Esegue l'intero grafo LangGraph V2 senza checkpointer Postgres e ritorna
    lo stato finale.

    Non viene usato alcun mock: tutti i nodi (sanitizer, extractor, mapper,
    evaluator, calculator, delivery) girano con dipendenze reali.
    Il delivery adapter di default è ConsoleAdapter — non richiede webhook.

    Parameters
    ----------
    lead:
        Caso del Golden Dataset da processare.

    Returns
    -------
    AgentState
        Stato finale dopo la conclusione del grafo (o dopo hitl_interrupt se
        confidence < soglia dopo i retry massimi).
    """
    graph = build_graph(checkpointer=None)
    thread_id: str = f"eval-{lead.lead_id}-{uuid.uuid4().hex[:8]}"
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = _build_initial_state(lead)
    final_state: AgentState = await graph.ainvoke(initial_state, config=config)
    return final_state


# ── LLM Judge ─────────────────────────────────────────────────────────────────

_JUDGE_SYSTEM: str = (
    "Sei un valutatore esperto di sistemi AI per la qualificazione di lead B2B. "
    "Ricevi: "
    "(1) il testo sanitizzato del lead — la PII è già sostituita con [REDACTED]; "
    "(2) i servizi estratti dal sistema tramite LLM; "
    "(3) le voci di catalogo associate tramite ricerca semantica pgvector. "
    "Valuta due metriche INDIPENDENTI su scala [0.0, 1.0]: "
    "recall_score — quanta parte dei bisogni espressi nel testo è stata CATTURATA "
    "dall'estrazione (0.0 = nulla catturato, 1.0 = tutti i bisogni catturati); "
    "precision_score — quanto le voci di catalogo mappate sono PERTINENTI rispetto "
    "ai bisogni estratti (0.0 = tutte errate o irrilevanti, 1.0 = tutte corrette). "
    "Se mapped_services è vuoto assegna precision_score = 0.0. "
    "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido, senza markdown, senza testo aggiuntivo: "
    '{"recall_score": <float 0-1>, "precision_score": <float 0-1>, '
    '"missed_needs": [<str>], "wrong_mappings": [<str>], "reasoning": "<str max 200 chars>"}'
)


def _build_judge_user_prompt(
    sanitized_text: str,
    extracted_services: list[str],
    mapped_services: list[dict[str, Any]],
) -> str:
    """
    Costruisce il prompt utente per il giudice LLM.

    SICUREZZA PII: questo metodo riceve SOLO ``sanitized_text`` (output di
    SanitizerNode), mai il ``raw_text`` originale del lead.
    """
    mapped_names: list[str] = [
        str(m.get("service", m)) for m in mapped_services
    ]
    return (
        f"Testo sanitizzato del lead:\n{sanitized_text}\n\n"
        f"Servizi estratti:\n{json.dumps(extracted_services, ensure_ascii=False)}\n\n"
        f"Voci di catalogo mappate (pgvector):\n{json.dumps(mapped_names, ensure_ascii=False)}\n\n"
        "Valuta recall_score e precision_score e restituisci il JSON richiesto."
    )


@dataclass
class JudgeVerdict:
    """Risultato strutturato della valutazione del giudice LLM."""

    recall_score: float
    precision_score: float
    missed_needs: list[str] = field(default_factory=list)
    wrong_mappings: list[str] = field(default_factory=list)
    reasoning: str = ""


def _parse_judge_response(raw: str) -> JudgeVerdict:
    """
    Deserializza la risposta del giudice LLM in un ``JudgeVerdict``.

    Robusto a code-fence, whitespace e JSON parziale — non crasha mai.
    In caso di parse error ritorna scores = 0.0 con reasoning = 'parse_error'.
    """
    text: str = raw.strip()
    if text.startswith("```"):
        lines: list[str] = text.splitlines()
        inner: list[str] = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)

    data: dict[str, Any] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start: int = text.find("{")
        end: int = text.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    if not data:
        return JudgeVerdict(recall_score=0.0, precision_score=0.0, reasoning="parse_error")

    return JudgeVerdict(
        recall_score=min(1.0, max(0.0, float(data.get("recall_score", 0.0)))),
        precision_score=min(1.0, max(0.0, float(data.get("precision_score", 0.0)))),
        missed_needs=list(data.get("missed_needs", [])),
        wrong_mappings=list(data.get("wrong_mappings", [])),
        reasoning=str(data.get("reasoning", "")),
    )


async def _call_judge(
    sanitized_text: str,
    extracted_services: list[str],
    mapped_services: list[dict[str, Any]],
) -> JudgeVerdict:
    """
    Invoca il giudice LLM tramite endpoint OpenAI-compatible (Ollama/Groq).

    Riceve SOLO ``sanitized_text``: nessun PII raggiunge il modello giudice.
    Usa lo stesso endpoint e modello configurati per il sistema (LLM_BASE_URL,
    LLM_MODEL_NAME) con temperature=0 per massima riproducibilità.

    Parameters
    ----------
    sanitized_text:
        Testo del lead con PII già mascherata da SanitizerNode.
    extracted_services:
        Servizi estratti dall'ExtractorNode.
    mapped_services:
        Voci di catalogo mappate dal MapperNode (pgvector).

    Returns
    -------
    JudgeVerdict
        Score di recall e precision con motivazione.
    """
    settings = get_settings()
    user_prompt: str = _build_judge_user_prompt(
        sanitized_text, extracted_services, mapped_services
    )
    url: str = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.llm_model_name,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    raw_content: str = response.json()["choices"][0]["message"]["content"]
    return _parse_judge_response(raw_content)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:  # type: ignore[return]
    """
    Pulisce la cache di get_settings() prima e dopo ogni eval per garantire
    l'isolamento tra i test (soprattutto in caso di monkeypatch da conftest padre).
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lead",
    GOLDEN_DATASET,
    ids=[g.lead_id for g in GOLDEN_DATASET],
)
async def test_lead_judge_scores(lead: GoldenLead) -> None:
    """
    Esegue il grafo completo su un singolo lead e verifica che il giudice LLM
    assegni recall_score >= min_recall_score e precision_score >= min_precision_score.

    Il giudice riceve SOLO il ``sanitized_text`` dallo stato finale — nessuna PII.
    """
    final_state: AgentState = await _run_full_graph(lead)

    # Campi dello stato V2 da valutare.
    sanitized_text: str = final_state.get("sanitized_text", "")
    extracted: list[str] = final_state.get("extracted_services", [])
    mapped: list[dict[str, Any]] = final_state.get("mapped_services", [])
    confidence: float = final_state.get("confidence_score", 0.0)

    assert sanitized_text, (
        f"[{lead.lead_id}] sanitized_text vuoto — SanitizerNode non ha girato correttamente."
    )

    # Il giudice valuta recall e precision senza vedere il raw_text.
    verdict: JudgeVerdict = await _call_judge(sanitized_text, extracted, mapped)

    print(
        f"\n[{lead.lead_id}] recall={verdict.recall_score:.2f} "
        f"precision={verdict.precision_score:.2f} "
        f"confidence_graph={confidence:.2f}\n"
        f"  extracted : {extracted}\n"
        f"  mapped    : {[m.get('service') for m in mapped]}\n"
        f"  missed    : {verdict.missed_needs}\n"
        f"  wrong     : {verdict.wrong_mappings}\n"
        f"  reasoning : {verdict.reasoning}"
    )

    assert verdict.recall_score >= lead.min_recall_score, (
        f"[{lead.lead_id}] recall {verdict.recall_score:.2f} < soglia {lead.min_recall_score:.2f}. "
        f"Bisogni mancati: {verdict.missed_needs}. Estratti: {extracted}"
    )
    assert verdict.precision_score >= lead.min_precision_score, (
        f"[{lead.lead_id}] precision {verdict.precision_score:.2f} < "
        f"soglia {lead.min_precision_score:.2f}. "
        f"Mapping errati: {verdict.wrong_mappings}. "
        f"Mappati: {[m.get('service') for m in mapped]}"
    )


async def test_judge_passrate() -> None:
    """
    Test aggregato: almeno ``GLOBAL_PASS_RATE`` (70%) dei lead deve superare
    ENTRAMBE le soglie individuali (recall e precision).

    Tollera falsi negativi del giudice su singoli casi senza bloccare la suite.
    Usa lo stesso meccanismo del Binario B (eval pass-rate §4.5 di TESTING_PLAN.md).
    """
    results: list[tuple[str, bool, JudgeVerdict]] = []

    for lead in GOLDEN_DATASET:
        final_state: AgentState = await _run_full_graph(lead)
        sanitized_text: str = final_state.get("sanitized_text", "")
        extracted: list[str] = final_state.get("extracted_services", [])
        mapped: list[dict[str, Any]] = final_state.get("mapped_services", [])

        verdict: JudgeVerdict = await _call_judge(sanitized_text, extracted, mapped)
        passed: bool = (
            verdict.recall_score >= lead.min_recall_score
            and verdict.precision_score >= lead.min_precision_score
        )
        results.append((lead.lead_id, passed, verdict))

    n_passed: int = sum(1 for _, ok, _ in results if ok)
    rate: float = n_passed / len(results) if results else 0.0

    report_lines: list[str] = [
        f"  {lid}: {'PASS' if ok else 'FAIL'} | "
        f"recall={v.recall_score:.2f} precision={v.precision_score:.2f} | "
        f"{v.reasoning[:100]}"
        for lid, ok, v in results
    ]
    report: str = "\n".join(report_lines)

    print(f"\n[judge passrate] {n_passed}/{len(results)} = {rate:.0%}\n{report}")

    assert rate >= GLOBAL_PASS_RATE, (
        f"Pass-rate giudice {rate:.0%} < soglia globale {GLOBAL_PASS_RATE:.0%}.\n{report}"
    )
