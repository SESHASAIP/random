# Phase 4 Step 1 Output Schema
## Beam Search Warm-Start Package

**Version:** 1.0  
**Date:** 2026-08-27

This is the only output of Phase 4 Stage 1.  
It is the input that seeds MCTS.  
It does **not** contain the final lifted Pattern.

---

## Output JSON

```json
{
  "document_id": "doc_007",
  "entity_name": "risk-type",
  "ground_truth": "<known target string>",
  "beam_width": 12,
  "max_depth": 4,
  "finished_paths": [
    {
      "path_id": "beam_fin_001",
      "actions": [
        {"type": "add_candidate", "candidate_id": "cand_001"},
        {"type": "transform", "name": "lower"},
        {"type": "add_candidate", "candidate_id": "cand_002"},
        {"type": "transform", "name": "concat_hyphen_space"}
      ],
      "used_candidate_ids": ["cand_001", "cand_002"],
      "used_candidates": [
        {
          "candidate_id": "cand_001",
          "role": "administrator",
          "raw_value": "<verbatim span from doc>",
          "source_section_id": "sec_4_1_2",
          "section_description": "<phase 1 short analysis>",
          "span_start": 14210,
          "span_end": 14229
        },
        {
          "candidate_id": "cand_002",
          "role": "risk_level",
          "raw_value": "<verbatim span from doc>",
          "source_section_id": "sec_7_3",
          "section_description": "<phase 1 short analysis>",
          "span_start": 50112,
          "span_end": 50121
        }
      ],
      "transforms": ["lower", "concat_hyphen_space"],
      "reconstructed": "<string built by the recipe>",
      "score": 0.93,
      "score_breakdown": {
        "string_similarity": 0.96,
        "token_coverage": 1.0,
        "fragment_hit_rate": 1.0,
        "candidate_evidence_avg": 0.82,
        "simplicity": 0.56
      },
      "exact_match": true,
      "depth": 4
    }
  ],
  "alive_partials": [
    {
      "path_id": "beam_liv_014",
      "actions": [
        {"type": "add_candidate", "candidate_id": "cand_001"},
        {"type": "transform", "name": "title"}
      ],
      "used_candidate_ids": ["cand_001"],
      "unused_candidate_ids": ["cand_002", "cand_003"],
      "reconstructed": "<partial string>",
      "score": 0.41,
      "exact_match": false,
      "depth": 2
    }
  ],
  "promising_candidates": ["cand_001", "cand_002"],
  "promising_transforms": ["concat_hyphen_space", "lower"],
  "stats": {
    "states_expanded": 186,
    "states_pruned_by_dedup": 44,
    "finished_count": 3,
    "alive_count": 12
  }
}
```

Placeholder strings only. Do not hard-code domain values.

---

## Field rules

### Top level

| Field | Meaning |
|-------|---------|
| `finished_paths` | Complete or exact-match recipes. Ranked by score. These are the best short explanations Beam found. |
| `alive_partials` | Unfinished high-scoring states. MCTS expands these. |
| `promising_candidates` | Candidate ids that appeared often / in high-scoring paths. Restricts MCTS action set. |
| `promising_transforms` | Transform names that actually helped. Restricts MCTS action set. |
| `beam_width` / `max_depth` | Config used, kept for audit. |

### Each path

| Field | Rule |
|-------|------|
| `actions` | Ordered recipe. Only `add_candidate` or `transform`. |
| `used_candidates` | Copy the Phase 3 provenance onto the path: raw_value, section, spans in the **full original txt**. |
| `reconstructed` | String after applying the recipe in order. |
| `exact_match` | `reconstructed.strip() == ground_truth.strip()` |
| `span_start` / `span_end` | Still whole-file offsets from Phase 3. Beam does not invent new spans. |

### What this output is not

- Not a lifted Pattern
- Not the full document graph
- Not new values extracted from the document
- Not an LLM summary

---

## What MCTS should read

```text
finished_paths        → seed as high-prior children
alive_partials        → expandable nodes
promising_candidates  → primary action set
promising_transforms  → primary transform set
used_candidates       → provenance for later Pattern lifting
```

If `finished_paths` already contains `exact_match: true`, MCTS confirms and Phase 6 can lift that path.  
If not, MCTS starts from `alive_partials` instead of an empty root.
