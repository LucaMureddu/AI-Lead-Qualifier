"""
ingestion/models.py
--------------------
Pydantic schemas and LangGraph state for the IngestionEngine.

Design principles
-----------------
- ``ServiceItem`` is the canonical unit: every source format (CSV/JSON/Excel)
  must map to it.  All fields are typed and validated at construction time.
- ``ServiceCatalog`` is the aggregate produced at the end of a pipeline run.
- ``IngestionState`` is the LangGraph TypedDict threaded through every node.
  Fields annotated with ``operator.add`` are append-only (fan-in safe);
  all others are last-write-wins.
- ``tenant_id`` is present at every level to enforce multi-tenant isolation:
  separate ChromaDB collections, separate SQLite rows, separate log prefixes.
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import TypedDict


# ── Price type enum ───────────────────────────────────────────────────────────

class PriceType(str, Enum):
    """
    Tipologia formale del prezzo di un ServiceItem.

    FIXED    — prezzo fisso noto (price IS NOT NULL AND price >= 0)
    FREE     — servizio gratuito (price = 0.0, scelta esplicita)
    VARIABLE — prezzo su richiesta / da preventivare (price IS NULL)

    Invariante DB (CHECK constraint 004_hybrid_pricing):
        FREE     ⟹ price = 0.0
        FIXED    ⟹ price IS NOT NULL AND price >= 0
        VARIABLE ⟹ price IS NULL
    """

    FIXED = "FIXED"
    FREE = "FREE"
    VARIABLE = "VARIABLE"


# ── Canonical service item ────────────────────────────────────────────────────

class ServiceItem(BaseModel):
    """
    A single normalised service entry, ready for storage in ChromaDB.

    Confidence
    ----------
    The ``confidence`` field carries the LLM's self-assessed certainty for the
    field mapping.  Items below the pipeline's threshold are flagged for human
    review before finalisation.
    """

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique item ID (auto-generated).",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owner tenant — used to scope ChromaDB collections.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Human-readable service name.",
    )
    description: Optional[str] = Field(
        default=None,
        description="Free-text description of the service.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Service family / product line (e.g. 'Cloud', 'Consulting').",
    )
    price: Optional[float] = Field(
        default=0.0,
        description=(
            "Unit price in the stated currency.  Must be ≥ 0.  "
            "None ⟺ VARIABLE (da preventivare).  "
            "Coerced by the hybrid-pricing model_validator: "
            "VARIABLE → None, FREE → 0.0, FIXED → must be non-None and ≥ 0."
        ),
    )
    price_type: PriceType = Field(
        default=PriceType.FIXED,
        description=(
            "Tipologia formale del prezzo. Governa l'invariante DB. "
            "Se non fornito esplicitamente dall'LLM, viene inferito: "
            "price is None ⇒ VARIABLE, altrimenti FIXED. "
            "FREE solo su conferma esplicita dell'utente."
        ),
    )
    currency: str = Field(
        default="EUR",
        description="ISO 4217 currency code.",
    )
    unit: Optional[str] = Field(
        default=None,
        description="Billing unit: 'hour' | 'month' | 'project' | 'license' | 'user'.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="LLM mapping confidence [0, 1].  Items below threshold are flagged.",
    )
    raw_data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Original source row — preserved for audit and re-normalisation.",
    )
    flagged: bool = Field(
        default=False,
        description="True when the item requires human review before finalisation.",
    )
    flag_reason: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of why the item was flagged.",
    )
    ingested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of when this item was created.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("price")
    @classmethod
    def price_non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None  # "da preventivare" / unresolvable price strings → None
        if v < 0:
            raise ValueError(f"price must be ≥ 0, got {v}")
        return round(v, 4)

    @field_validator("currency")
    @classmethod
    def currency_uppercase(cls, v: str) -> str:
        normalised = v.strip().upper()
        if len(normalised) != 3:  # noqa: PLR2004
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got '{v}'")
        return normalised

    @field_validator("unit")
    @classmethod
    def unit_lowercase(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v else None

    @model_validator(mode="before")
    @classmethod
    def infer_price_type(cls, values: Any) -> Any:
        """
        Inferenza del price_type quando non fornito esplicitamente dall'LLM.

        Regola:
          - price_type già presente  → invariato (l'utente sa cosa vuole)
          - price_type assente/None  → price is None ⇒ VARIABLE, altrimenti FIXED
          - FREE viene inferito SOLO se esplicitamente impostato dall'utente nella
            tabella interattiva; non viene mai inferito automaticamente.
        """
        if not isinstance(values, dict):
            return values
        if not values.get("price_type"):
            values["price_type"] = (
                PriceType.VARIABLE if values.get("price") is None else PriceType.FIXED
            )
        return values

    @model_validator(mode="after")
    def enforce_hybrid_pricing_invariant(self) -> "ServiceItem":
        """
        Coercizione post-validazione dell'invariante C(price_type, price):

          FREE     → price forzato a 0.0
          VARIABLE → price forzato a None  (nessun sentinel)
          FIXED    → price deve essere non-None e ≥ 0  (loud failure)
        """
        if self.price_type == PriceType.FREE:
            self.price = 0.0
        elif self.price_type == PriceType.VARIABLE:
            self.price = None
        elif self.price_type == PriceType.FIXED:
            if self.price is None:
                raise ValueError(
                    "price_type=FIXED richiede un prezzo non-None e >= 0. "
                    "Usa price_type=VARIABLE per i prezzi su richiesta."
                )
        return self

    @model_validator(mode="after")
    def auto_flag_low_confidence(self) -> "ServiceItem":
        """Auto-flag items where confidence is below a minimum safe threshold."""
        _MIN_AUTO_CONFIDENCE: float = 0.5
        if self.confidence < _MIN_AUTO_CONFIDENCE and not self.flagged:
            self.flagged = True
            self.flag_reason = (
                f"Auto-flagged: confidence {self.confidence:.2f} < {_MIN_AUTO_CONFIDENCE}"
            )
        return self

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def is_computable(self) -> bool:
        """
        True se l'item ha un valore sommabile nel preventivo.

        Fonte canonica dell'informazione "computabilità": i dict in mapped_services
        ne sono la proiezione serializzata tramite price_type == 'VARIABLE'.
        """
        return self.price_type != PriceType.VARIABLE


# ── Aggregate catalogue ───────────────────────────────────────────────────────

class ServiceCatalog(BaseModel):
    """
    Immutable snapshot of a tenant's catalogue after a completed ingestion run.
    Written to SQLite for audit; items are stored individually in ChromaDB.
    """

    tenant_id: str
    items: List[ServiceItem]
    source_file: str
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_items: int = Field(default=0)
    flagged_count: int = Field(default=0)
    validation_error_count: int = Field(default=0)

    @model_validator(mode="after")
    def compute_counts(self) -> ServiceCatalog:
        self.total_items = len(self.items)
        self.flagged_count = sum(1 for i in self.items if i.flagged)
        return self


# ── Ingestion result (lightweight, serialisable) ──────────────────────────────

class IngestionResult(BaseModel):
    """
    Returned by the API after a completed (or rejected) ingestion run.
    Does NOT include the full item list to keep the response size bounded.
    """

    tenant_id: str
    source_file: str
    status: Literal["completed", "rejected", "error"]
    total_items: int
    flagged_count: int
    validation_errors: List[str]
    message: Optional[str] = None


# ── LangGraph shared state ────────────────────────────────────────────────────

class IngestionState(TypedDict):
    """
    Mutable state threaded through every node of the IngestionGraph.

    Reducer annotations
    -------------------
    Fields annotated ``Annotated[List[…], operator.add]`` use list-append
    semantics: each node *extends* the list rather than replacing it.
    This is safe for fan-in and for multi-chunk accumulation.

    All other fields are last-write-wins (standard LangGraph default).

    Chunk loop
    ----------
    The graph processes ``raw_chunks`` one batch at a time.
    ``current_chunk_index`` advances by 1 after each NormalizerNode pass.
    The conditional router after NormalizerNode checks whether
    ``current_chunk_index < len(raw_chunks)`` to decide whether to loop
    or proceed to ValidationNode.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    tenant_id: str
    """Owner of this ingestion run — scopes all DB writes."""

    source_file: str
    """Absolute path to the file being ingested."""

    file_format: Literal["csv", "json", "xlsx", "pdf"]
    """Explicit format hint (not inferred from extension)."""

    # ── Chunking ──────────────────────────────────────────────────────────────
    raw_chunks: List[List[Dict[str, Any]]]
    """List of batches; each batch is a list of raw row dicts."""

    current_chunk_index: int
    """Index of the batch currently being (or about to be) normalised."""

    # ── Processing ────────────────────────────────────────────────────────────
    normalized_items: Annotated[List[ServiceItem], operator.add]
    """
    Accumulates normalised items across all chunk iterations.
    Append-only — NormalizerNode extends this list, never replaces it.
    """

    validation_errors: Annotated[List[str], operator.add]
    """
    Accumulates validation error messages.
    Append-only — ValidationNode extends this list.
    """

    flagged_items: List[ServiceItem]
    """
    Subset of ``normalized_items`` that require human review.
    Set (replaced) by ValidationNode after all chunks are processed.
    """

    confidence_score: float
    """
    Average confidence across all normalised items in the current run.
    Updated by ValidationNode.
    """

    # ── Human-in-the-Loop ────────────────────────────────────────────────────
    approved: Optional[bool]
    """
    Set by ApprovalNode after the human resumes the graph.
    ``None`` before the approval step; ``True`` or ``False`` after.
    """

    review_feedback: Optional[str]
    """
    Free-text feedback from the human reviewer (populated on rejection).
    Passed back to NormalizerNode if the graph is re-invoked for correction.
    """

    # ── Observability ─────────────────────────────────────────────────────────
    sse_logs: Annotated[List[str], operator.add]
    """Append-only stream of human-readable log lines for SSE delivery."""

    error: Optional[str]
    """Set by any node that catches an unrecoverable exception."""
