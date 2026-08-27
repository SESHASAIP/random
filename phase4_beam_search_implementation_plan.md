# Phase 4 Stage 1 — Beam Search Implementation Plan
## Constrained Bottom-Up Recipe Search Before MCTS

**Version:** 1.0  
**Date:** 2026-08-27  
**Position:** After Phase 3 candidate generation, before MCTS

---

## 1. Goal

Find short, high-quality derivation recipes that reconstruct the known ground-truth value from Phase 3 candidates + generic transforms.

Beam Search is **not** the final solver. It is the primitive filter / warm-start:

- Discover strong short combinations quickly
- Reduce the action space for MCTS
- Seed MCTS with finished paths and alive partials

Success for this stage:

- Exact or near-exact reconstructions when the recipe is short
- The necessary candidates and transforms survive in the beam
- MCTS does not have to start from an empty root over all 30 candidates

---

## 2. Inputs

From Phase 3 output (`phase3_output_schema.md`):

| Input | Use |
|-------|-----|
| `document_id` | Isolation |
| `entity_name` | Logging / later Pattern |
| `ground_truth` | Scoring target |
| `candidates[]` | Only legal SubEntities Beam may use |
| `filtered_graph` | Context for scoring / later lifting, not for expanding new values |

Also:

| Input | Use |
|-------|-----|
| `transform_library` | Generic unary + binary transforms only |
| `beam_config` | Width, depth, score weights |

Hard rules:

- Beam may use **only** Phase 3 candidates
- No full-document scan
- No hard-coded domain strings inside transforms
- Example names in this file are placeholders

---

## 3. Outputs

Full output schema: `phase4_beam_output_schema.md`

This package is the warm-start input to MCTS. It contains:

- `finished_paths` — complete / exact-match recipes, with Phase 3 provenance and whole-file spans
- `alive_partials` — high-scoring unfinished states for MCTS to expand
- `promising_candidates` / `promising_transforms` — restricted action set for MCTS
- `stats` — how many states were expanded / deduped

---

## 4. State Representation

```python
@dataclass
class BeamState:
    path_id: str
    actions: List[Action]                 # ordered recipe
    used_candidate_ids: Set[str]
    reconstructed: str                    # current string
    value_stack: List[str]                # for binary transforms
    score: float = 0.0
    exact_match: bool = False

    def dedup_key(self) -> tuple:
        return (self.reconstructed, frozenset(self.used_candidate_ids))
```

An `Action` is either:

```python
{"type": "add_candidate", "candidate_id": "cand_001"}
{"type": "transform", "name": "lower"}            # unary
{"type": "transform", "name": "concat_hyphen_space"}  # binary
```

---

## 5. Legal Actions

At a state, only these expansions are allowed.

### Add candidate
Legal if `candidate_id` is not already used.

Effect:
- push `raw_value` (or `normalized_value` if already set) onto `value_stack`
- if `reconstructed` is empty, set it to that value

### Unary transform
Legal if `value_stack` is non-empty and the transform is not a no-op on the top value.

Effect:
- pop one value, apply transform, push result
- `reconstructed =` new top

### Binary transform
Legal if `value_stack` has ≥ 2 values, **or** current string is non-empty and there is at least one unused candidate that can be consumed in the same step.

Preferred binary form for this problem:
- current reconstructed string + one unused candidate

Effect:
- `reconstructed = transform(current, candidate.raw_value)`
- mark that candidate used

### Banned
- reuse of the same candidate
- `lower` then `lower`, `strip` on already-stripped text
- inventing a value not in Phase 3
- opening new graph sections

This is constrained beam search: illegal moves are masked, not scored.

---

## 6. Algorithm

```text
beam = [empty_state]
finished = []
seen = set()

for depth in 1 .. max_depth:
    nxt = []
    for state in beam:
        for action in legal_actions(state, candidates, transforms):
            child = apply(action, state)
            if child.dedup_key() in seen:
                continue
            seen.add(child.dedup_key())
            child.score = beam_score(child, ground_truth, candidates)
            if child.reconstructed == ground_truth or is_terminal(child):
                child.exact_match = (child.reconstructed.strip() == ground_truth.strip())
                finished.append(child)
            else:
                nxt.append(child)
    beam = top_k(nxt, beam_width)

return finished, beam
```

Notes:
- Keep finished and alive lists separate
- An exact match at depth 2 must not be dropped just because a longer path has a similar raw score
- Dedup by `(reconstructed, used_candidate_set)` so equivalent recipes do not fill the beam

---

## 7. Scoring Function

```text
score =
    0.40 * string_similarity(reconstructed, ground_truth)
  + 0.20 * token_coverage
  + 0.15 * fragment_hit_rate
  + 0.15 * mean(evidence_score of used candidates)
  + 0.10 * simplicity
```

Definitions:

- `string_similarity`: normalized Levenshtein or token-sort ratio, 0–1
- `token_coverage`: fraction of ground-truth tokens present in reconstructed
- `fragment_hit_rate`: fraction of ground-truth fragments (split on space / hyphen / underscore) that already appear
- `simplicity`: `1 / (1 + n_actions * 0.25)`
- exact match bonus: `+0.15`, capped at 1.0

No LLM inside the beam loop.  
Scoring is deterministic so the stage stays fast and inspectable.

---

## 8. Recommended Config

```python
@dataclass
class BeamConfig:
    beam_width: int = 12
    max_depth: int = 4
    max_candidates: int = 20          # use top 20 Phase 3 candidates
    exact_match_stops_path: bool = True
    dedup: bool = True
    k_output_finished: int = 10
    k_output_partials: int = 12
```

Why these numbers:
- Width 12 keeps both pieces of a composite value even if one looks weak alone
- Depth 4 covers pick → normalize → join
- Top 20 candidates keeps branching manageable
- Longer / weirder recipes are MCTS’s job

---

## 9. How Beam Hands Off to MCTS

MCTS root is **not** empty over all candidates.

Seed MCTS with:

1. Each `finished_path` as a high-prior child
2. Each `alive_partial` as an expandable node with its remaining unused candidates
3. Restricted action set:
   - `promising_candidates`
   - `promising_transforms`
   - plus a small exploration tail from unused Phase 3 candidates if needed

If Beam already produced an exact match with a short clean recipe, MCTS can confirm and pass it to Pattern Lifting.  
If Beam only got close, MCTS explores dropped branches.

---

## 10. Worked Shape of a Good Path

Depth 1: add candidate A whose raw value overlaps a ground-truth fragment  
Depth 2: unary normalize A  
Depth 3: add candidate B  
Depth 4: binary join A and B

That is the typical successful beam path.  
If a recipe needs 7 steps, do not raise `max_depth` first. Let MCTS take it.

---

## 11. Implementation Structure

```python
def run_beam_search(
    phase3_output: dict,
    transform_library: List[Transformation],
    config: BeamConfig = BeamConfig()
) -> dict:
    candidates = phase3_output["candidates"][:config.max_candidates]
    ground_truth = phase3_output["ground_truth"]

    empty = BeamState(path_id="root", actions=[], used_candidate_ids=set(),
                      reconstructed="", value_stack=[])
    finished, alive = beam_loop(empty, candidates, transform_library, ground_truth, config)

    return {
        "document_id": phase3_output["document_id"],
        "entity_name": phase3_output["entity_name"],
        "ground_truth": ground_truth,
        "beam_width": config.beam_width,
        "max_depth": config.max_depth,
        "finished_paths": serialize(sorted(finished, key=lambda s: s.score, reverse=True)[:config.k_output_finished]),
        "alive_partials": serialize(sorted(alive, key=lambda s: s.score, reverse=True)[:config.k_output_partials]),
        "promising_candidates": frequent_ids(finished + alive),
        "promising_transforms": frequent_transform_names(finished + alive),
    }
```

---

## 12. Tests

1. Two candidates that join into the ground truth with one concat transform → Beam must finish with exact match
2. Candidate needs a case transform before it matches a fragment → unary path survives
3. Duplicate reconstructed strings → only one stays in the beam
4. Illegal reuse of a candidate → never generated
5. Beam width 1 drops the weaker first piece → width 12 still keeps it
6. No LLM calls during the loop

---

## 13. Checklist

- [ ] `BeamState` + `Action` models
- [ ] `legal_actions()` with no-op and reuse bans
- [ ] `apply()` + `reconstruct` via value stack
- [ ] Deterministic `beam_score()`
- [ ] Dedup key
- [ ] Main loop with separate finished / alive lists
- [ ] Warm-start JSON for MCTS
- [ ] Unit tests above
- [ ] Wire to Phase 3 output schema

---

## 14. Design Principles

- Beam is constrained recipe search, not text generation
- Score against the known ground truth, not a model logit
- Keep it deterministic and fast
- Compress the action space for MCTS
- Do not hunt in the full document
- Do not hard-code domain values

---

*This is the definitive specification for Phase 4 Stage 1 (Beam Search).*
"""
