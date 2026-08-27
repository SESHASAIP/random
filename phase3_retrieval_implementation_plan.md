# Phase 3 Implementation Plan
## Candidate Generation from Graph + Original Text + Supporting Section Text

**Version:** 1.3  
**Date:** 2026-08-27  
**Position in pipeline:** After Phase 2 (Graph Construction), before Phase 4 (Beam Search + MCTS)

---

## 1. Goal

Produce a ranked list of **real SubEntities** that Phase 4 can actually combine and transform.

Phase 3 is not “find similar sections.”  
Phase 3 is:

> Using the graph as a map, open the original text and supporting section text, and extract every raw value that might be needed to reconstruct the ground truth.

Success metric: **recall of necessary pieces**, not exact reconstruction.  
Reconstruction belongs to Phase 4.

---

## 2. Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| `document_id` | Phase 0 | Isolation key |
| `entity_name` | Task | What we are explaining |
| `ground_truth` | Task | Target string to reconstruct |
| `doc_graph` | Phase 2 | Structure, embeddings, flags, short analysis, parent/child links |
| `original_txt` | Phase 0 | Source of truth for exact spans and offsets |
| `section_texts` | Graph nodes / Phase 1 | Full `raw_text` of each section plus neighbor text |

Every graph node must already carry:

- `section_id`, `title`, `level`, `parent_id`
- `raw_text`
- `short_analysis`
- `preliminary_signals`
- `contains_parties`, `contains_risk_language`, `potential_entities`
- `embedding`
- `start_char`, `end_char`

---

## 3. Outputs

```python
@dataclass
class SubEntity:
    role: str
    raw_value: str
    normalized_value: str = ""
    source_section_id: str
    section_title: str
    section_description: str          # short_analysis
    local_context: str                # supporting text around the span
    evidence_score: float
    span_start: int                   # char offset in the FULL original_txt
    span_end: int                     # char offset in the FULL original_txt
    retrieval_signals: Dict[str, float] = field(default_factory=dict)
    supporting_section_ids: List[str] = field(default_factory=list)
```

### What `span` means

**Span = location in the whole input document text (`original_txt`), not a local index inside a section snippet.**

```text
original_txt[span_start:span_end] == raw_value
```

- `span_start` / `span_end` are character offsets from the beginning of the full original `.txt`
- You still **search only inside the graph-selected section window**:
  `original_txt[section.start_char:section.end_char]`
- After you find the match in that window, you store the offsets against the **whole file**
- Do not store 0-based positions relative to the section snippet
- Later phases can jump straight to the exact place in the original document

Phase 3 returns:

```python
List[SubEntity]   # ranked, typically 15–40 candidates
```

These are the **only building blocks** MCTS may use.

---

## 4. High-Level Architecture

```text
entity_name + ground_truth
        │
        ▼
Query Builder
  - full query
  - fragment queries
  - role queries from Phase 1 flags
        │
        ▼
Stage A: High-recall retrieval on GRAPH
  - BM25 on title + raw_text + short_analysis
  - Dense search on embeddings
  - Metadata filter / boost from flags
  - Hierarchy / neighbor expansion
  - RRF fusion → wide section pool
        │
        ▼
Stage B: Rerank sections
  - Cross-encoder or LLM judge
  - Uses title + short_analysis + excerpt of raw_text
        │
        ▼
Stage C: Read supporting text and extract spans
  - Open selected section raw_text
  - Also open parent / sibling / child / cross-ref sections
  - Extract real values from original_txt
  - Attach role, context, offsets
        │
        ▼
Ranked SubEntity list → Phase 4
```

---

## 5. Why All Three Inputs Are Required

| Input | If missing | Failure mode |
|-------|------------|--------------|
| Graph only | No exact values | MCTS gets summaries, not real strings |
| Original txt only | No structure | Search is blind to hierarchy and Phase 1 flags |
| Section text only | Weak global search | Misses fragments and cross-section pieces |

Correct split of labor:

- **Graph** = where to look. This is the high-lifting from Phases 1–2. All retrieval happens on graph sections, flags, neighbors, and embeddings.
- **Supporting section text** = what the value actually says. Read only sections the graph selected, plus their graph neighbors.
- **Original txt** = verify exact characters and store offsets **inside those section windows only**.

**Hard rule:**  
Do **not** fall back to a full-document scan.  
If the graph + supporting section text did not surface a piece, Phase 3 does not go hunting through the raw file. That would throw away the structure built in Phases 1–2.

---

## 6. Stage A — High-Recall Section Retrieval

### 6.1 Query Builder

Build multiple queries from the task, not one:

```python
queries = {
    "full": f"{entity_name} {ground_truth}",
    "gt_only": ground_truth,
    "entity_only": entity_name,
    "fragments": split_ground_truth(ground_truth),   # tokens / chunks
    "roles": roles_from_phase1_flags(doc_graph)      # administrator, risk_level, ...
}
```

`split_ground_truth()` should be conservative:
- split on space, hyphen, underscore
- keep original fragments
- do **not** over-decompose at this stage

### 6.2 Parallel retrievers

**Sparse (BM25)**  
Index per section:

```text
title + short_analysis + raw_text + preliminary_signals
```

**Dense**  
Use the existing node embedding.  
Query embedding = embed(full query) and also embed(each fragment).

**Metadata / flag boost**  
If a section has `contains_parties` or `contains_risk_language` or matching `potential_entities`, apply a boost.  
This is stronger than parent-title heuristics.

**Neighbor expansion**  
If a section is retrieved, also add:
- parent
- children
- explicit cross-ref targets from the graph

### 6.3 RRF Fusion — What It Means and How to Compute It

**RRF = Reciprocal Rank Fusion.**

We run several retrievers (BM25, dense, fragment queries, role queries).  
Each one returns its own ranked list of sections.  
Their raw scores are not comparable:

- BM25 scores can be 0 to a large number
- Dense cosine similarity is roughly -1 to 1

So we **do not mix the raw scores**. We only mix **ranks**.

#### Formula

For each section \(d\):

\[
\mathrm{RRF}(d) = \sum_i \frac{1}{k + \mathrm{rank}_i(d)}
\]

- \(i\) = one retriever
- \(\mathrm{rank}_i(d)\) = position of section \(d\) in that retriever’s list (1 = best)
- \(k\) = constant, use **60**
- If a retriever did not return that section, it contributes **0**

Higher RRF score wins.

#### Worked example

| Rank | BM25 | Dense | Fragment query |
|------|------|-------|----------------|
| 1 | sec_7_3 | sec_4_1 | sec_7_3 |
| 2 | sec_4_1 | sec_7_3 | sec_12 |
| 3 | sec_9 | sec_12 | sec_4_1 |

With \(k = 60\):

- `sec_7_3` = \(1/61 + 1/62 + 1/61 \approx 0.0488\)
- `sec_4_1` = \(1/62 + 1/61 + 1/63 \approx 0.0484\)
- `sec_12` = \(0 + 1/63 + 1/62 \approx 0.0320\)
- `sec_9`  = \(1/63 + 0 + 0 \approx 0.0159\)

Fused order: `sec_7_3`, `sec_4_1`, `sec_12`, `sec_9`.

A section that appears near the top in **multiple** lists gets boosted. That is the whole point.

#### Code to use

```python
from collections import defaultdict

def rrf_fuse(ranked_lists, k=60, top_n=40):
    """
    ranked_lists: list of ordered section_id lists, one per retriever.
    Example:
      [
        ["sec_7_3", "sec_4_1", "sec_9"],      # BM25
        ["sec_4_1", "sec_7_3", "sec_12"],     # dense
        ["sec_7_3", "sec_12", "sec_4_1"],     # fragment
      ]
    """
    scores = defaultdict(float)
    for ranked in ranked_lists:
        for rank, section_id in enumerate(ranked, start=1):
            scores[section_id] += 1.0 / (k + rank)

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [section_id for section_id, _ in fused[:top_n]], dict(scores)
```

#### How it fits Phase 3

```text
bm25_list     = retrieve_bm25(queries, doc_graph)
dense_list    = retrieve_dense(queries, doc_graph)
fragment_list = retrieve_fragments(queries, doc_graph)
role_list     = retrieve_by_phase1_flags(doc_graph)

fused_section_ids, rrf_scores = rrf_fuse(
    [bm25_list, dense_list, fragment_list, role_list],
    k=60,
    top_n=40
)
```

Keep a **wide pool**: top 30–50 sections.  
Optimize this stage for recall. Rerank and span extraction come next.

#### Rules

- Do not try to normalize BM25 and cosine into one handmade formula. Use RRF.
- Keep `k=60` unless you later tune it on labeled docs.
- Store each section’s `rrf_score` on the candidate. It is reused later in `evidence_score`.
- Neighbor expansion happens **after** fusion: if a fused section is kept, also add its parent/children/cross-refs into the pool before rerank.

---

## 7. Stage B — Section Rerank

Rerank the fused pool with a cross-encoder or a small LLM judge.

**Input to reranker:**
- `entity_name`
- `ground_truth`
- section `title`
- `short_analysis`
- first ~800–1500 characters of `raw_text`

**Output:**
```json
{
  "section_id": "sec_7_3",
  "relevance": 0.86,
  "why": "Contains risk classification language that may supply one piece of the target value"
}
```

Keep top 10–20 sections for span extraction.

Do not drop a section just because it does not look like the full ground-truth string.  
A section can still contain one necessary piece.

---

## 8. Stage C — Supporting Text + Span Extraction

This is the step that creates real benefit for MCTS.

### 8.1 Assemble supporting text

For each surviving section:

```text
supporting_text =
    parent.title + parent.raw_text   (optional, truncated)
    + current.title + current.raw_text
    + selected children / siblings if graph says they are related
```

Also keep a pointer into `original_txt[start_char:end_char]`.

### 8.2 Extract candidate spans

Use a hybrid extractor:

1. **Deterministic first**
   - exact fragment matches from ground_truth
   - quoted names, title-case entities, defined terms
   - Phase 1 `preliminary_signals`

2. **LLM extraction second** (structured, one call per section or per small batch)

Prompt sketch:

```text
Entity: {entity_name}
Ground truth: {ground_truth}

Section title: {title}
Section analysis: {short_analysis}

Section text:
{supporting_text}

Extract every phrase in this text that could be a raw piece used to reconstruct the ground truth.
Do not invent text. Only copy spans that appear verbatim.

Return JSON:
{
  "candidates": [
    {
      "raw_value": "...",
      "role": "administrator | risk_level | party | other",
      "why": "...",
      "local_context": "1-2 surrounding sentences"
    }
  ]
}
```

### 8.3 Bind offsets to original_txt (section window only)

`original_txt` is used only as a verifier and offset map for sections already chosen by the graph.

For every extracted `raw_value`:
- search **only** inside that section’s window: `original_txt[section.start_char:section.end_char]`
- if the section has supporting neighbors, search those neighbor windows too
- store `span_start`, `span_end` as offsets into the **full** `original_txt` (not local to the snippet)
- invariant: `original_txt[span_start:span_end] == raw_value`
- if multiple matches exist in the same window, keep the one whose local context best matches the extraction

If the value cannot be found in the selected section windows, **drop it**.  
Do not search the rest of the file.

No invented values leave Phase 3.  
No full-document fallback.

---

## 9. Scoring a SubEntity

```text
evidence_score =
    0.25 * section_rrf_score
  + 0.25 * rerank_score
  + 0.20 * fragment_overlap(raw_value, ground_truth)
  + 0.15 * flag_support            # Phase 1 flags / potential_entities
  + 0.15 * extraction_confidence
```

Keep more candidates than you think you need.  
Typical keep list: **15–40 SubEntities**.

Deduplicate near-identical spans.  
Keep both `"High Risk"` and `"high risk"` if both appear; let Phase 4 decide transforms.

---

## 10. Function Contract

```python
def phase3_generate_candidates(
    document_id: str,
    entity_name: str,
    ground_truth: str,
    doc_graph: DocGraph,
    original_txt: str,
    config: Phase3Config = Phase3Config()
) -> List[SubEntity]:
    queries = build_queries(entity_name, ground_truth, doc_graph)
    section_pool = hybrid_retrieve_and_rrf(doc_graph, queries, config)
    ranked_sections = rerank_sections(
        section_pool, entity_name, ground_truth, doc_graph
    )
    supporting = gather_supporting_text(ranked_sections, doc_graph)
    spans = extract_spans(supporting, entity_name, ground_truth)
    subentities = bind_offsets_and_score(spans, original_txt, doc_graph)
    return dedupe_and_truncate(subentities, top_k=config.top_k)
```

---

## 11. Config Defaults

```python
@dataclass
class Phase3Config:
    rrf_k: int = 60
    section_pool_size: int = 40
    sections_after_rerank: int = 15
    top_k_subentities: int = 30
    neighbor_hops: int = 1
    max_supporting_chars: int = 4000
    use_llm_span_extractor: bool = True
    drop_if_not_in_original_txt: bool = True
```

---

## 12. What Phase 4 Receives

Each SubEntity must already contain:

- real `raw_value` copied from the document
- guessed `role`
- `source_section_id`
- `section_description` (Phase 1 short analysis)
- `local_context` from supporting text
- exact offsets into `original_txt`

Phase 4 should **not** have to reopen the full document to know what the value was or where it came from.

---

## 13. Failure Modes to Guard Against

| Failure | Guard |
|---------|-------|
| Only one section retrieved for a composite value | Multi-query + neighbor expansion + wide pool |
| Values taken from summaries, not source | Bind every span to original_txt inside the section window |
| Missing piece after graph retrieval | Do not scan the full file. Widen graph recall instead: more neighbors, more queries, larger RRF pool || Transformed GT looks unlike raw text | Fragment queries + BM25 + span extractor |
| Too few candidates | Recall-first, top_k=30 |
| Too much junk | Rerank + dedupe, not aggressive first-stage pruning |
| Hallucinated values | Drop anything not found verbatim in original_txt |

---

## 14. Evaluation

Measure Phase 3 independently from MCTS:

- **Piece recall:** did the candidate list contain every raw piece needed for the gold derivation?
- **Section recall:** were the gold source sections in the retrieved pool?
- **Span precision:** are extracted values verbatim from original_txt?
- **Offset accuracy:** do spans point to the right occurrence?

A Phase 3 run can be “successful” even if it does not reconstruct the ground truth.  
It only needs to give Phase 4 the ingredients.

---

## 15. Implementation Checklist

- [ ] Confirm graph nodes expose `raw_text`, flags, embeddings, offsets
- [ ] Query builder (full + fragments + role queries)
- [ ] BM25 index over title + analysis + raw_text
- [ ] Dense retrieval over node embeddings
- [ ] Flag / hierarchy boost + neighbor expansion
- [ ] RRF fusion
- [ ] Section reranker
- [ ] Supporting-text assembler
- [ ] Deterministic span finder + LLM span extractor
- [ ] Offset binder against original_txt
- [ ] Scoring, dedupe, top-k
- [ ] Unit tests:
  - composite value split across two sections
  - value that only matches after later transform
  - rejected hallucinated span

---

## 16. Design Principles

- Graph navigates. Text proves.
- Retrieve wide, extract precise.
- Never send section blobs to MCTS. Send values with provenance.
- Do not hard-code domain strings. Fragments come from the current ground_truth at runtime.
- Phase 3 optimizes recall of pieces. Phase 4 optimizes combination + transformation.

---

*This is the combined Phase 3 specification: enterprise hybrid retrieval + graph structure + original document text + supporting section text + span-level candidate generation.*
"""
