# Piano di Migrazione V2 → V3 — Pricing Ibrido Tipizzato

> Obiettivo: promuovere il flag implicito `metadata.is_on_request` a una colonna
> tipizzata di prima classe `price_type` (`FIXED` / `FREE` / `VARIABLE`),
> con l'invariante di consistenza garantita a livello di motore PostgreSQL
> tramite `CHECK` constraint.

---

## 0. Decisioni di progetto (lette dal confronto)

| Decisione | Scelta | Conseguenza |
|---|---|---|
| Storage del prezzo VARIABLE | **`price NUMERIC NULL` + `CHECK`** (no sentinel `-1.0`) | `VARIABLE ⟺ price IS NULL`. Aggregati SQL corretti by-default. `is_computable` derivato da `price_type`, non da un magic number. |
| Scope di questo giro | **Solo piano scritto** | Nessuna modifica al codice in questo step. |
| Dati esistenti | **Tabella svuotata** (solo test) | Migration semplice: nessun backfill euristico, nessun rischio di marcare per errore degli `0.0`. |

### Perché `NULL + CHECK` e non il sentinel `-1.0` del PDF

Il PDF (§2.2) giustifica `-1.0` sostenendo che `NULL` "invalida `SUM(price)`".
In PostgreSQL questo non è esatto: `SUM()` **ignora i `NULL`** nativamente, quindi
`SELECT SUM(price)` resta corretto. Il rischio reale del sentinel è opposto: in una
colonna `NOT NULL` ogni query aggregata che dimentichi `WHERE price_type != 'VARIABLE'`
sottrae silenziosamente `-1.0`. Con `NULL`, l'aggregato è corretto automaticamente e
non esiste alcun magic number da nascondere. Manteniamo comunque l'astrazione
`is_computable` per disaccoppiare i nodi LangGraph dalla rappresentazione fisica.

---

## 1. Modello dati target

### Invariante (versione NULL)

```
C(price_type, price) ⟺
      (price_type = 'FREE'     ∧ price = 0.0)
    ∨ (price_type = 'FIXED'    ∧ price IS NOT NULL ∧ price ≥ 0.0)
    ∨ (price_type = 'VARIABLE' ∧ price IS NULL)
```

### Schema tabella `catalogue_items` (post-migrazione)

```sql
id          UUID         PRIMARY KEY DEFAULT gen_random_uuid()
tenant_id   TEXT         NOT NULL
service     TEXT         NOT NULL
price       FLOAT        NULL          -- ⚠️ era NOT NULL
price_type  VARCHAR(20)  NOT NULL DEFAULT 'FIXED'   -- ⬅️ nuova colonna
description TEXT
embedding   VECTOR(768)
metadata    JSONB        NOT NULL DEFAULT '{}'::jsonb
-- UNIQUE (tenant_id, service)         invariato
-- HNSW + B-Tree(tenant_id)            invariati
-- CONSTRAINT chk_hybrid_pricing_logic ⬅️ nuovo
```

Nota: la colonna `service` resta `service` (non rinominata in `service_name` come nel
PDF) per non rompere `vector_store.py`, `catalogue_routes.py` e i test esistenti.
La rinomina è cosmetica e fuori scope.

---

## 2. Migration Alembic `004_hybrid_pricing`

File nuovo: `backend/migrations/versions/004_hybrid_pricing.py`
(`down_revision = "003_audit_log"`)

**`upgrade()`** — ordine:

1. `ALTER TABLE catalogue_items ADD COLUMN price_type VARCHAR(20) NOT NULL DEFAULT 'FIXED';`
   (il `DEFAULT` rende l'operazione sicura anche su righe residue; la tabella è
   comunque vuota.)
2. `ALTER TABLE catalogue_items ALTER COLUMN price DROP NOT NULL;`
3. Aggiungere il vincolo:
   ```sql
   ALTER TABLE catalogue_items
     ADD CONSTRAINT chk_hybrid_pricing_logic CHECK (
         (price_type = 'FREE'     AND price = 0.0) OR
         (price_type = 'FIXED'    AND price IS NOT NULL AND price >= 0.0) OR
         (price_type = 'VARIABLE' AND price IS NULL)
     );
   ```
4. (Opzionale) `ALTER COLUMN price_type DROP DEFAULT;` se si preferisce forzare
   l'esplicitazione del tipo a livello applicativo. **Sconsigliato** ora: il default
   `'FIXED'` protegge gli INSERT legacy che non passano ancora `price_type`.

**`downgrade()`** — ordine inverso: drop constraint → `ALTER COLUMN price SET NOT NULL`
(richiede prima `UPDATE ... SET price = 0.0 WHERE price IS NULL`) → drop column `price_type`.

---

## 3. Modelli Pydantic — `backend/ingestion/models.py`

Estendere il `ServiceItem` **esistente** (non crearne uno nuovo): conserva
`confidence`, `flagged`, `raw_data` ecc.

Aggiunte:

```python
from enum import Enum

class PriceType(str, Enum):
    FIXED    = "FIXED"
    FREE     = "FREE"
    VARIABLE = "VARIABLE"
```

Campo nuovo su `ServiceItem`:

```python
price_type: PriceType = Field(
    default=PriceType.FIXED,
    description="Tipologia formale del prezzo. Governa l'invariante DB.",
)
```

`price` resta `Optional[float]` (già così oggi — comodo: `None` ⇒ VARIABLE).

`@model_validator(mode="after")` — **coercizione in post-validazione** (adattata a NULL):

```
- Se price_type == FREE      → price = 0.0
- Se price_type == VARIABLE  → price = None        (nessun sentinel)
- Se price_type == FIXED     → richiede price is not None and price >= 0,
                               altrimenti raise ValueError (loud failure)
```

Inferenza del tipo all'ingestion (quando l'LLM/parser non lo fornisce esplicito):
`price is None ⇒ VARIABLE`, altrimenti `FIXED`. `FREE` solo su conferma esplicita
dell'utente nella tabella interattiva (Step 3 del tuo flusso). Questa regola va
incapsulata in un validator `mode="before"` o nel NormalizerNode.

Proprietà astratta (disaccoppia i nodi dalla rappresentazione fisica):

```python
@property
def is_computable(self) -> bool:
    """True se l'item ha un valore sommabile nel preventivo."""
    return self.price_type != PriceType.VARIABLE
```

Edge case da coprire nei test (i 3 casi limite del PDF):
1. `price_type=VARIABLE, price=999` → `price` forzato a `None`.
2. `price_type=FREE, price=999` → `price` forzato a `0.0`.
3. `price_type=FIXED, price=None` → `ValueError`.

---

## 4. Persistenza — `backend/database/vector_store.py`

- **`upsert_items`**: aggiungere `price_type` alla tupla INSERT e a
  `ON CONFLICT (tenant_id, service) DO UPDATE SET ... price_type = EXCLUDED.price_type`.
  Mappare `price=None` correttamente (asyncpg passa `None` → `NULL`, ok con la nuova colonna).
- **`similarity_search`**: aggiungere `price_type` alla `SELECT` e includerlo nei
  metadati del `Document` restituito (`row["price_type"]`).

---

## 5. Flusso di qualificazione (LangGraph)

Il punto delicato: i nodi operano su `mapped_services: List[Dict]` (dict grezzi),
**non** su istanze `ServiceItem`. Quindi `is_computable` come property non è
direttamente accessibile a runtime sui dict. Due opzioni:

- **(A, consigliata)** Il mapper porta `price_type` nei dict; i nodi controllano
  `entry["price_type"] == "VARIABLE"`. Minimo attrito, nessuna idratazione.
- **(B)** Idratare ogni dict in `ServiceItem` dentro il calculator per usare
  `.is_computable`. Più pulito concettualmente, ma costo e ridondante.

Scegliendo (A), il flag `is_on_request` viene **sostituito da `price_type`** in tutta
la catena. Mappa puntuale delle modifiche:

| File | Oggi (V2) | V3 |
|---|---|---|
| `ingestion/graph.py:706` | `"is_on_request": item.price is None` | `"price_type": item.price_type.value` |
| `agents/mapper.py:204` | `best.metadata.get("is_on_request")` | `best.metadata.get("price_type", "FIXED")` |
| `agents/calculator.py:30,67` | `entry.get("is_on_request")` | `entry.get("price_type") == "VARIABLE"` |
| `agents/delivery.py:48` | `svc.get("is_on_request")` | `svc.get("price_type") == "VARIABLE"` |

`calculator_node`: nel `_sum_prices`, saltare gli entry con `price_type == "VARIABLE"`
(il loro `price` è `None`, quindi `float(None)` esploderebbe — la guardia va messa
**prima** della somma). Gli stessi entry alimentano `on_request_services`.

`core/state.py`: il contratto del dict in `mapped_services` passa da
`{service, price, is_on_request}` a `{service, price, price_type}`. Aggiornare la
docstring del campo.

---

## 6. API Admin — `backend/api/catalogue_routes.py`

- `_PATCHABLE_COLUMNS`: aggiungere `"price_type"` → `{"service", "price", "description", "price_type"}`.
- `CatalogueItemPatch` / `CatalogueItemResponse` / `CatalogueItemPatchResponse`:
  aggiungere `price_type: Optional[PriceType]` e rendere `price: Optional[float]`.
- **Gestione errore CHECK**: un PATCH incoerente (es. `price_type=VARIABLE` con
  `price=150`) farà fallire la transazione con `asyncpg.CheckViolationError`.
  Intercettarlo e restituire `422` con messaggio chiaro invece di un `500`.
- La validazione `Field(ge=0)` su `price` va resa condizionale (un VARIABLE manda
  `price=None`): spostare la coerenza price/price_type in un `model_validator` del
  modello di patch, riusando la stessa logica di `ServiceItem`.

---

## 7. Frontend (fuori scope — solo contratto da congelare)

Non implementato in questo giro, ma il backend deve esporre ciò che servirà alla
tabella interattiva Alpine.js (Step 2–3 del tuo flusso):

- La preview di ingestion deve restituire `price_type` per ogni riga.
- Riga rossa ⟺ `price_type === 'VARIABLE'`.
- Il menu a tendina manda un PATCH con uno dei tre valori; `FIXED` accompagnato dal
  float, `FREE`/`VARIABLE` senza prezzo.

---

## 8. Test (`backend/tests/`)

- `tests/unit/test_models.py`: aggiungere i 3 casi di coercizione (§3) +
  verifica `is_computable` per i tre tipi.
- `tests/unit/test_calculator.py`: **riscrivere** i fixture — oggi usano
  `is_on_request` e `price=0.0`. Nuovi casi:
  - `FIXED` (price>0) → entra nel totale.
  - `FREE` (price=0) → contribuisce 0, **non** in `on_request`.
  - `VARIABLE` (price=None) → escluso dal totale, finisce in `on_request_services`,
    e **non** solleva su `float(None)`.
  - Invarianza matematica: `total == somma dei soli FIXED/FREE`.
- `tests/unit/test_catalogue_api.py`: PATCH che viola il CHECK → 422.
- Integration: verificare che la migration applichi il constraint e che un INSERT
  incoerente sia respinto dal DB (test su `vector_store` con Testcontainers).

---

## 9. Ordine di esecuzione consigliato

1. Migration `004` (schema + CHECK).
2. `PriceType` + campo + validator + `is_computable` in `ingestion/models.py`.
3. `vector_store.py` (upsert + select).
4. `ingestion/graph.py` (set `price_type` invece di `is_on_request`).
5. `mapper.py` → `calculator.py` → `delivery.py` (catena di lettura).
6. `core/state.py` (docstring del contratto dict).
7. `catalogue_routes.py` (PATCH + gestione `CheckViolationError`).
8. Test (unit prima, poi integration).
9. (Step successivo) tabella interattiva frontend.

---

## 10. Rischi e punti aperti

- **Doppia rappresentazione del "computabile"**: dopo il V3 convivono `is_computable`
  (property sul modello) e il check `price_type == 'VARIABLE'` sui dict. Documentare
  che la property è la fonte canonica e i dict ne sono la proiezione serializzata.
- **`FIXED` con `price = 0`**: rimane valido e indistinguibile da `FREE` a livello di
  totale (entrambi contribuiscono 0). Semanticamente diversi (FREE è una scelta
  esplicita). Va bene, ma è bene saperlo: l'invariante li ammette entrambi.
- **Embedding**: invariato. Il PATCH di `price_type` da solo non deve rigenerare
  l'embedding (l'embedding dipende da `service`/`description`, non dal prezzo).
  Verificare che la logica di re-embedding async in `catalogue_routes.py` non si
  attivi su un cambio del solo `price_type`.
- **Migrazione del JSONB legacy**: i vecchi `metadata.is_on_request` non vengono
  letti più da nessuno dopo il V3; dato che la tabella è svuotata, nessuna pulizia
  necessaria. Su un futuro DB con dati reali servirebbe invece uno script di
  backfill `metadata.is_on_request == true → price_type='VARIABLE'`.
