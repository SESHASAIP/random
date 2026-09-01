# Alignment and Transform Tools Plan

**Version:** 1.1  
**Date:** 2026-08-31

Order: Valentine → PROSE → TDE → DataXFormer.

Valentine pairs primitives to ground-truth pieces.  
PROSE learns a string pattern from those pairs.  
TDE searches a transform catalog plus mapping tables.  
DataXFormer stores TDE output in an append-only lookup table, queries it, and applies the winner.

`cITCO`, `high risk` are placeholders only.

---

## Shared input from the document side

```python
@dataclass
class Primitive:
    primitive_id: str
    document_id: str
    raw_value: str
    role_hint: str                   # optional, from section flags
    source_section_id: str
    section_title: str
    section_description: str
    local_context: str
    span_start: int                  # offsets in full original_txt
    span_end: int
```

```python
@dataclass
class GroundTruthTask:
    document_id: str
    entity_name: str
    ground_truth: str                # e.g. "cITCO- high risk"
    fragments: list[str]             # split on -, /, |, whitespace
```

Invariant: `original_txt[span_start:span_end] == raw_value`.

---

## Tool 1 — Valentine

**Kind:** Python library + thin wrapper.  
**Package:** `valentine` (`pip install valentine`).  
**Matchers used:** `Coma(use_instances=True)` first. Fallback `DistributionBased` if Coma is empty.

Valentine matches **columns** across two tables.  
The wrapper builds those two tables and then pairs **values** inside the matched columns.

### Input contract

```python
@dataclass
class ValentineInput:
    document_id: str
    entity_name: str
    primitives: list[Primitive]      # table A rows
    fragments: list[str]             # table B values
    ground_truth: str
```

Table A — one column per role hint, cells are primitive raw values.

```text
admin_or_party     risk_or_label     other
Citco Fund Svcs    High              ...
```

Table B — one column per fragment slot.

```text
gt_part_0    gt_part_1
cITCO        high risk
```

If role hints are missing, Table A is a single column `primitive` with every raw value.

### Output contract

```python
@dataclass
class ValentinePair:
    pair_id: str
    document_id: str
    entity_name: str
    primitive: Primitive
    fragment: str
    source_column: str
    target_column: str
    column_score: float              # Valentine matcher score
    value_score: float               # instance score inside the column pair
    match_score: float               # combined
    matcher: str                     # coma | distribution_based

@dataclass
class ValentineOutput:
    document_id: str
    entity_name: str
    column_matches: list[dict]       # raw Valentine matches
    pairs: list[ValentinePair]       # value-level pairs that survive threshold
    unmatched_primitives: list[str]
    unmatched_fragments: list[str]
```

### Logic

1. Build `df_primitives` and `df_fragments` as pandas DataFrames.
2. `matches = valentine_match([df_primitives, df_fragments], Coma(use_instances=True))`.
3. Keep column matches with score ≥ `col_threshold` (start `0.3`).
4. For each kept column pair, score every primitive cell against every fragment cell:
   - exact / casefold
   - token Jaccard
   - prefix / acronym
5. `match_score = 0.4 * column_score + 0.6 * value_score`.
6. Greedy one-to-one assign: highest score first, each primitive and each fragment used at most once.
7. Drop pairs below `pair_threshold` (start `0.45`).
8. If Coma returns no column matches, rerun with `DistributionBased` and take the union.

Valentine does not invent transforms. It only emits pairs.

### Library vs custom

| Piece | Source |
|---|---|
| Column matching | `valentine` |
| DataFrame construction, value scoring, greedy assign | custom wrapper |

---

## Tool 2 — PROSE

**Kind:** custom Python synthesizer.  
**Why custom:** Microsoft PROSE SDK is C# / .NET and is no longer released. There is no supported `pip` FlashFill package. Implement a small string DSL in Python that covers the transforms this corpus actually needs.

Optional later: call PROSE via `pythonnet` if a licensed SDK is available. Same input/output contract.

### Input contract

```python
@dataclass
class IOExample:
    input_value: str                 # primitive.raw_value
    output_value: str                # valentine fragment

@dataclass
class ProseInput:
    document_id: str
    entity_name: str
    pairs: list[ValentinePair]       # from Tool 1
    extra_inputs: list[str]          # other primitives to run the learned pattern on
```

Examples fed to the synthesizer = `[(pair.primitive.raw_value, pair.fragment) for pair in pairs]`.

### Output contract

```python
@dataclass
class ProsePattern:
    pattern_id: str
    pattern_name: str                # identity | case_fold | acronym | substring | concat | regex_extract | compose
    pattern_spec: dict               # executable params, no domain literals
    source_code: str                 # python callable body or expression
    score: float                     # fraction of examples satisfied
    examples_used: list[IOExample]

@dataclass
class ProseApply:
    input_value: str
    output_value: str
    ok: bool

@dataclass
class ProseOutput:
    document_id: str
    entity_name: str
    pattern: ProsePattern | None
    applies: list[ProseApply]
    unsatisfied_examples: list[IOExample]
```

### DSL (what the synthesizer may emit)

| Op | Meaning |
|---|---|
| identity | `x` |
| case_fold | `lower` / `upper` / `title` |
| acronym | initials of tokens, optional case |
| substring | `x[i:j]` or regex group |
| concat | join two ops with a literal separator taken from ground_truth |
| regex_extract | first match of a pattern induced from examples |
| compose | pipeline of the above |

No hardcoded names. Separators and patterns come only from the examples.

### Logic

1. Require at least one Valentine pair. Zero pairs → `pattern=None`.
2. Try ops in order: identity, case_fold, acronym, substring, regex_extract, concat of two primitives.
3. An op is legal only if it produces `output_value` for every example it is tested on.
4. Rank: more examples satisfied, then simpler pattern (fewer ops).
5. Keep the top pattern. Run it on `extra_inputs`. Record applies.
6. If no pattern satisfies all examples, keep the best partial and list `unsatisfied_examples`.

PROSE does not look at the document text. It only sees string pairs.

### Library vs custom

| Piece | Source |
|---|---|
| Synthesis + DSL interpreter | custom Python |
| Optional PROSE SDK | C# interop, same contract |

---

## Tool 3 — TDE

**Kind:** custom.  
**Why custom:** Transform-Data-by-Example is a Microsoft research/Excel system. No public Python package. Reimplement the *search-by-example* idea against a local catalog: PROSE patterns already learned + the growing mapping table.

TDE does not crawl GitHub. Catalog is local only: prior PROSE patterns plus the mapping table.

### Input contract

```python
@dataclass
class TDEInput:
    document_id: str
    entity_name: str
    ground_truth: str
    fragments: list[str]
    primitives: list[Primitive]
    valentine_pairs: list[ValentinePair]
    prose_output: ProseOutput | None
    catalog_patterns: list[ProsePattern]     # patterns learned on prior docs
    catalog_rows: list[ReferenceRow]         # DataXFormer reference table so far
```

### Output contract

```python
@dataclass
class TDEExample:
    input_value: str
    output_value: str

@dataclass
class TDEResult:
    tde_run_id: str
    document_id: str
    entity_name: str
    examples: list[TDEExample]
    pattern_name: str
    pattern_spec: dict
    applied_input: str
    applied_output: str
    score: float
    source: str                              # prose_current | catalog_pattern | catalog_row
    source_section_id: str
    section_title: str
    section_description: str
    local_context: str
    span_start: int
    span_end: int
    created_at: str

@dataclass
class TDEOutput:
    document_id: str
    entity_name: str
    results: list[TDEResult]                 # one per resolved fragment / pair
    unresolved_fragments: list[str]
```

`TDEOutput.results` is exactly what Module 1 of DataXFormer ingests.

### Logic

Search order, first hit that covers the example set wins.

1. **Current PROSE pattern.** If `prose_output.pattern` satisfies the Valentine pairs, emit one `TDEResult` per pair. `source=prose_current`. Copy span and section from the primitive.
2. **Catalog patterns.** For each prior `ProsePattern`, run it on current primitive values. If output equals a fragment, emit `TDEResult`. `source=catalog_pattern`.
3. **Catalog rows.** Lookup `input_norm` / `output_norm` in the reference table (exact, then fuzzy cap). If a current primitive aligns, emit `TDEResult` using that row’s `pattern_*`. `source=catalog_row`. Span still from the current primitive.
4. Anything not covered goes to `unresolved_fragments`.

Do not invent an output that is not a fragment or a catalog `output_raw`.

Score:

- `prose_current`: PROSE pattern score
- `catalog_pattern`: fraction of current pairs it satisfies
- `catalog_row`: reference-table confidence / tde_score

### Library vs custom

| Piece | Source |
|---|---|
| Example search over patterns + mapping table | custom |
| Persistence of results | DataXFormer Module 1 `ingest_tde` |

---

## Tool 4 — DataXFormer

**Kind:** custom.  
**Why custom:** original DataXFormer is a 2015–2016 research system. No pip package. No weights. Local append-only table only.

One tool. Three modules.

1. Reference table
2. Query matching
3. Apply

TDE is upstream. DataXFormer does not run TDE. It persists `TDEOutput.results`, looks them up, and applies the winner on the current document.

### Module 1 — Reference table

Append-only lookup store of TDE results. No harvest. No synthesis.

```python
@dataclass
class ReferenceRow:
    row_id: str
    tde_run_id: str
    document_id: str
    entity_name: str
    input_raw: str
    input_norm: str
    output_raw: str
    output_norm: str
    pattern_name: str
    pattern_spec: dict
    tde_score: float
    source_section_id: str
    section_title: str
    section_description: str
    local_context: str
    span_start: int
    span_end: int
    verified: bool                   # True after Module 3 exact match
    created_at: str
```

Functions:

```text
ingest_tde(tde_output: TDEOutput, index) -> list[ReferenceRow]
index.append(rows) -> None
index.lookup(keys, entity_name=None, fuzzy=False) -> list[ReferenceRow]
index.mark_verified(row_ids) -> None
```

Ingest input: `TDEOutput` from Tool 3.

Ingest logic:

1. Normalize `input_*` and `output_*`: NFKC, lower, collapse space, strip outer punctuation, keep inner hyphens.
2. One `ReferenceRow` per `TDEResult`, plus one row per `TDEExample` pair if not already covered.
3. Copy span and section from the TDE result. `verified=False`.
4. `index.append(rows)`.

Unique key: `(input_norm, output_norm, document_id, tde_run_id, span_start)`.

Append-only. Same mapping from a new document is a new row. Contradictions are kept.

Indexes: `input_norm`, `output_norm`, `entity_name`, `document_id`, trigram/prefix on `input_raw`.

Module 1 output: the table. That is what Module 2 queries.

### Module 2 — Query matching

Look up current values against the reference table. Vote.

```python
@dataclass
class Query:
    document_id: str
    entity_name: str
    ground_truth: str
    fragments: list[str]
    extra_keys: list[str]            # verified keys from prior runs; empty on run one
    current_values: list[str]

@dataclass
class QueryResult:
    keys: list[str]
    hits: list[ReferenceRow]
    hits_by_key: dict[str, list[ReferenceRow]]
    unresolved_keys: list[str]

@dataclass
class VoteCandidate:
    key: str
    output_norm: str
    output_raw: str
    pattern_name: str
    pattern_spec: dict
    support_count: int               # distinct document_id
    support_docs: list[str]
    support_rows: list[str]
    confidence: float
    conflict: bool
```

Functions:

```text
query(q: Query, index) -> QueryResult
vote(hits_by_key, eligible_source_count) -> list[VoteCandidate]
```

Query keys = `fragments` ∪ normalized `current_values` ∪ `extra_keys`.

Query logic:

1. Exact lookup on `input_norm` and `output_norm`.
2. If a key has zero exact hits, fuzzy lookup on `input_raw` with a cap.
3. Bucket hits by key.
4. A hit from another document is evidence for `pattern_name` / `output_*`. It is not a span in the current document.

Vote logic, per key:

1. Group rows by `(output_norm, pattern_name)`.
2. `support_count` = distinct `document_id`.
3. `confidence = support_count / max(eligible_source_count, 1)`.
4. Winner = max support. Tie-break: `verified=True`, then higher mean `tde_score`.
5. `conflict=True` if runner-up support ≥ 0.5 × winner support.

Gates used by Module 3:

| Gate | Value |
|---|---|
| min_sources | 2 |
| min_confidence | 0.6 |
| conflict | block auto-apply |

First document: gates fail. Module 3 uses the current TDE result only.

Vote output: `list[VoteCandidate]`.

### Module 3 — Apply

Run the winning pattern on the current document. Lift the derivation Pattern. Mark rows verified.

```python
@dataclass
class FragmentApply:
    key: str
    used_row: ReferenceRow | None
    current_input: str | None
    transformed_value: str | None
    resolved: bool
    source: str                      # vote | local_tde | unresolved

@dataclass
class Reconstruction:
    document_id: str
    entity_name: str
    ground_truth: str
    separator: str
    fragments: list[FragmentApply]
    reconstructed: str
    exact_match: bool

@dataclass
class DerivationPattern:
    pattern_id: str
    entity_name: str
    ground_truth: str
    source_document_id: str
    components: list[ReferenceRow]
    combination: str
    intention_summary: str
    confidence: float
    reused_from_docs: list[str]
```

Functions:

```text
apply(...) -> Reconstruction
lift_pattern(reconstruction, entity_name) -> DerivationPattern | None
mark_verified(index, row_ids) -> None
```

Apply input: votes, local `TDEOutput`, current document inputs with spans, `ground_truth`, `document_id`, `entity_name`.

Apply logic, per ground-truth fragment:

1. If vote passes gates and a current input aligns with the winning `input_norm` → run `pattern_spec` on that current input. `source=vote`.
2. Else if local TDE covers this fragment → use local TDE output. `source=local_tde`.
3. Else unresolved.

Join with the separator taken from `ground_truth`.  
`exact_match` if reconstructed equals `ground_truth` after declared case transforms.

A component span must belong to the current `document_id`. Other documents do not donate spans.

Lift: if not `exact_match`, return `None`. Else emit `DerivationPattern` from applied components, current-document spans, ordered pattern names plus join, min confidence, and `reused_from_docs` excluding current `document_id`.

Write-back: `mark_verified` on rows used in an exact match. No new rows from Module 3. New rows only enter through Module 1 from TDE.

### DataXFormer runner

```text
rows  = ingest_tde(tde_output, index)
q     = Query(fragments=split(ground_truth), extra_keys=verified_keys(entity_name), current_values=...)
qr    = query(q, index)
votes = vote(qr.hits_by_key, eligible_source_count)
recon = apply(votes, tde_output, current_inputs, ground_truth, document_id)
pat   = lift_pattern(recon, entity_name)
mark_verified(index, exact_match_row_ids)
```

Isolation:

1. TDE and spans are per `document_id`.
2. DerivationPattern spans come only from the current `original_txt`.
3. Reference rows change pattern choice and confidence only.
4. Conflicting rows stay in the table and block auto-apply.

### Library vs custom

| Piece | Source |
|---|---|
| Reference table, query, vote, apply | custom SQLite + Python |
| Web tables / web forms from original DataXFormer | unused |

---

## Wiring

```text
primitives + fragments
        │
        ▼
[Valentine]  pairs (primitive ↔ fragment)
        │
        ▼
[PROSE]      pattern + applies
        │
        ▼
[TDE]        TDEResult[]   ← also reads catalog patterns + reference table
        │
        ▼
DataXFormer Module 1 ingest_tde → append-only reference table
        │
        ▼
DataXFormer Module 2 query + vote
        │
        ▼
DataXFormer Module 3 apply + pattern
```

Each tool is callable alone with the contracts above. Downstream tools tolerate empty upstream output: unmatched fragments stay unmatched, they are not guessed.

---

## Thresholds to start

| Name | Tool | Value |
|---|---|---|
| col_threshold | Valentine | 0.30 |
| pair_threshold | Valentine | 0.45 |
| prose min examples | PROSE | 1 |
| prose min score to accept | PROSE | 1.0 all examples, else keep partial |
| tde catalog fuzzy cap | TDE | 5 hits per key |
