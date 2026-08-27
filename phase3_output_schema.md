# Phase 3 Output Schema

**Version:** 1.0  
**Date:** 2026-08-27

Phase 3 does two jobs:

1. Extract ranked candidate values (`candidates`)
2. Compress the graph to only the sections those candidates came from (`filtered_graph`)

MCTS uses the candidate list as building blocks.  
The filtered graph is the supporting map around those values.  
Phase 4 should not receive the full document graph.

---

## Output JSON

```json
{
  "document_id": "doc_007",
  "entity_name": "risk-type",
  "ground_truth": "<the known target string>",
  "candidates": [
    {
      "candidate_id": "cand_001",
      "role": "administrator",
      "raw_value": "Citco Fund Services",
      "normalized_value": "",
      "source_section_id": "sec_4_1_2",
      "section_title": "Appointment of Administrator",
      "section_description": "Appoints the fund administrator and defines its duties.",
      "local_context": "The Administrator shall be Citco Fund Services...",
      "evidence_score": 0.84,
      "span_start": 14210,
      "span_end": 14229,
      "supporting_section_ids": ["sec_4_1", "sec_4_1_3"],
      "retrieval_signals": {
        "rrf": 0.0488,
        "rerank": 0.86,
        "fragment_overlap": 0.31,
        "flag_support": 1.0
      }
    },
    {
      "candidate_id": "cand_002",
      "role": "risk_level",
      "raw_value": "high risk",
      "normalized_value": "",
      "source_section_id": "sec_7_3",
      "section_title": "Risk Classification",
      "section_description": "Defines the internal risk category of the Borrower.",
      "local_context": "The Borrower is classified as high risk...",
      "evidence_score": 0.81,
      "span_start": 50112,
      "span_end": 50121,
      "supporting_section_ids": ["sec_7"],
      "retrieval_signals": {
        "rrf": 0.0471,
        "rerank": 0.83,
        "fragment_overlap": 0.72,
        "flag_support": 1.0
      }
    }
  ],
  "filtered_graph": {
    "kept_section_ids": ["sec_4_1", "sec_4_1_2", "sec_4_1_3", "sec_7", "sec_7_3"],
    "nodes": [
      {
        "section_id": "sec_4_1_2",
        "title": "Appointment of Administrator",
        "parent_id": "sec_4_1",
        "short_analysis": "Appoints the fund administrator and defines its duties.",
        "contains_parties": true,
        "contains_risk_language": false,
        "start_char": 14100,
        "end_char": 15840
      }
    ],
    "edges": [
      {"from": "sec_4_1", "to": "sec_4_1_2", "type": "parent_child"}
    ]
  }
}
```

Example values like `Citco Fund Services` / `high risk` are placeholders only.

---

## Field rules

### Top level

| Field | Meaning |
|-------|---------|
| `document_id` | Isolation key from Phase 0 |
| `entity_name` | Target entity being explained |
| `ground_truth` | Known final value to reconstruct |
| `candidates` | Ranked SubEntities for Phase 4 |
| `filtered_graph` | Compressed subgraph around those candidates |

### Each candidate

| Field | Rule |
|-------|------|
| `raw_value` | Copied verbatim from the document. Never paraphrased. |
| `span_start` / `span_end` | Character offsets in the **full original `.txt`**. Invariant: `original_txt[span_start:span_end] == raw_value` |
| `section_description` | Phase 1 short analysis of the source section |
| `local_context` | Nearby supporting section text |
| `role` | Guess only. Phase 4 / pattern lifting may refine it. |
| `evidence_score` | Combined retrieval + extraction confidence |
| `supporting_section_ids` | Neighbor sections used as context |

### Filtered graph

- Keep only surviving sections plus their graph neighbors
- Do **not** dump full `raw_text` for every kept node into this JSON
- Heavy text stays on the graph store and is referenced by `section_id`
- Edges keep parent/child and cross-ref links among kept nodes

---

## What Phase 4 receives

```text
candidates       → building blocks to combine and transform
filtered_graph   → context and provenance
original_txt     → only to verify spans if needed
```

Phase 3 compresses the search space to **candidate values + a small subgraph**, not to a bag of whole sections.

Typical size: **15–40 candidates**.  
Success = all raw pieces needed for the gold derivation are in `candidates`. Extra noise is acceptable. Missing a necessary piece is not.
