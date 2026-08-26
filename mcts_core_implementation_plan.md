# State-of-the-Art MCTS Core Logic Implementation Plan
## Reverse-Engineering Derivation Paths from Credit Agreement Document Graphs

**Version:** 1.2  
**Date:** 2026-08-25  
**Status:** Production-ready design for Phase 4 (Two-Stage Search: Beam → MCTS)

> **Note on examples:**  
> Throughout this document the strings `Citco`, `cITCO`, `high risk`, and the ground-truth `cITCO- high risk` are **only placeholders**.  
> The algorithm, transforms, prompts, and data models are fully generic and must work for any `entity_name` and any ground-truth value.

---

## 1. Purpose & Position in the Pipeline

This document defines the **exact** implementation of the MCTS core (Phase 4 of the overall pipeline).

It sits between:

- **Upstream (already built):**  
  Phase 0–2 → Document Graph  
  Phase 3 → Ranked list of candidate `SubEntity` objects (from Nearest-Neighbour retrieval)

- **Downstream:**  
  Phase 5 → Evidence Package  
  Phase 6 → Pattern Lifting (rich reusable Patterns)

**Ultimate goal of this module:**  
Given a document graph + candidate sub-entities + ground-truth value, discover the highest-quality derivation paths (values + transformations + combination structure + source context) that reconstruct the ground truth.

---

## 2. Inputs & Outputs

### Inputs (wired from previous phases)

| Input | Source | Description |
|-------|--------|-------------|
| `doc_graph` | Phase 2 | Traversable graph/tree of sections. Each node has: `id`, `title`, `raw_text`, `short_analysis`, `embedding`, `parent_id`, `children` |
| `candidates` | Phase 3 | Ranked `List[SubEntity]` — the only building blocks MCTS is allowed to use |
| `ground_truth` | User / task | Exact string we must reconstruct (e.g. `"cITCO- high risk"`) |
| `entity_name` | User / task | e.g. `"risk-type"` (used only for logging & final Pattern) |
| `document_id` | Phase 0 | Isolation key — every node and output is tagged with it |
| `transform_library` | Shared / static | Registry of allowed transformations |

### Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| `best_paths` | Phase 5 | Ordered list of high-scoring derivation paths (raw action sequences) |
| `patterns` | Phase 6 | Lifted rich `Pattern` objects (the real deliverable) |

---

## 3. Core Data Models (Must match overall plan)

### 3.1 SubEntity (comes from Phase 3)

```python
@dataclass
class SubEntity:
    role: str                          # "administrator", "risk_level", ...
    raw_value: str                     # "Citco"
    normalized_value: str              # "cITCO" (can be empty initially)
    source_section_id: str
    section_title: str
    section_description: str           # short_analysis from graph node
    local_context: str                 # surrounding text
    evidence_score: float              # from nearest-neighbour (0–1)
    span_start: int
    span_end: int
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### 3.2 Transformation

```python
@dataclass
class Transformation:
    name: str                          # "case_normalize", "concat_hyphen_space", ...
    description: str
    apply: Callable[[str, Optional[str]], str]   # pure function
    is_binary: bool = False            # True for concat-style transforms
```

### 3.3 MCTSNode

```python
@dataclass
class MCTSNode:
    node_id: str
    parent: Optional["MCTSNode"] = None
    children: List["MCTSNode"] = field(default_factory=list)
    
    # The action that created this node
    action: Optional[Union[SubEntity, Transformation]] = None
    
    # Full history from root (critical for reconstruction)
    path_history: List[Union[SubEntity, Transformation]] = field(default_factory=list)
    
    # Statistics
    visit_count: int = 0
    total_reward: float = 0.0
    
    # Actions still available from this node
    untried_actions: List[Union[SubEntity, Transformation]] = field(default_factory=list)
    
    @property
    def average_reward(self) -> float:
        return self.total_reward / self.visit_count if self.visit_count > 0 else 0.0
```

### 3.4 Pattern (final output after lifting)

```python
@dataclass
class Pattern:
    pattern_id: str
    document_id: str
    target_entity: str
    reconstructed_value: str
    confidence: float
    structure: str                     # human-readable recipe
    components: List[SubEntity]
    transforms_applied: List[Transformation]
    intention_summary: str
    source_section_ids: List[str]
```

---

## 4. Transformation Library (Generic)

These are the only transformations MCTS is allowed to apply by default.  
They are pure, deterministic, and recorded in the final Pattern.  

**Important:** `Citco`, `cITCO`, `high risk` etc. used elsewhere in this document are **only illustrative placeholders**. The library and all logic must work for any entity name and any ground-truth value.

```python
TRANSFORM_LIBRARY = [
    Transformation(
        name="lower",
        description="Convert to lower case",
        apply=lambda s, _: s.lower() if s else s,
        is_binary=False
    ),
    Transformation(
        name="upper",
        description="Convert to upper case",
        apply=lambda s, _: s.upper() if s else s,
        is_binary=False
    ),
    Transformation(
        name="title",
        description="Convert to title case",
        apply=lambda s, _: s.title() if s else s,
        is_binary=False
    ),
    Transformation(
        name="strip",
        description="Strip leading/trailing whitespace",
        apply=lambda s, _: s.strip() if s else s,
        is_binary=False
    ),
    Transformation(
        name="concat_hyphen_space",
        description="A + '- ' + B",
        apply=lambda a, b: f"{a}- {b}" if b is not None else a,
        is_binary=True
    ),
    Transformation(
        name="concat_hyphen",
        description="A + '-' + B",
        apply=lambda a, b: f"{a}-{b}" if b is not None else a,
        is_binary=True
    ),
    Transformation(
        name="concat_space",
        description="A + ' ' + B",
        apply=lambda a, b: f"{a} {b}" if b is not None else a,
        is_binary=True
    ),
    Transformation(
        name="concat_underscore",
        description="A + '_' + B",
        apply=lambda a, b: f"{a}_{b}" if b is not None else a,
        is_binary=True
    ),
    # Domain-specific normalizers should NOT be hard-coded.
    # They can be:
    #   1. Proposed dynamically by the LLM when the fixed library is insufficient
    #   2. Learned and promoted via the Shared Knowledge Layer from previous documents
]
```

**Rules:**
- Binary transforms consume two values.
- Unary transforms operate on one value.
- Never hard-code specific strings such as organization names or risk labels inside transforms.
- Special normalizations are discovered at runtime (LLM) or reused from previously successful Patterns.

---

## 5. MCTS Algorithm — Exact Implementation

### 5.1 Configuration

```python
@dataclass
class MCTSConfig:
    iterations: int = 2000
    exploration_constant: float = 1.414   # classic UCB1
    max_depth: int = 6
    beam_width_for_warmstart: int = 12    # used by Stage 1 Beam Search
    min_reward_to_keep: float = 0.75
    random_seed: int = 42
```

### 5.2 Stage 1 — Beam Search Warm-Start (Primitive Filter)

Before pure MCTS, run a fast Beam Search to:
1. Discover strong short combinations
2. Reduce the effective candidate pool
3. Seed the MCTS root with high-quality partial paths

```python
def beam_search_warmstart(
    candidates: List[SubEntity],
    transforms: List[Transformation],
    ground_truth: str,
    beam_width: int = 12,
    max_depth: int = 4
) -> List[List[Union[SubEntity, Transformation]]]:
    """
    Returns list of high-scoring partial paths.
    These paths are used to initialize children of the MCTS root.
    """
    # Implementation: classic beam search keeping top-k partial reconstructions
    # at each depth. Score with the same reward function used by MCTS.
    ...
```

### 5.3 Stage 2 — Full MCTS

#### 5.3.1 Selection (UCB1)

```python
def select(node: MCTSNode, C: float) -> MCTSNode:
    current = node
    while current.untried_actions == [] and current.children:
        current = max(
            current.children,
            key=lambda n: ucb1(n, C)
        )
    return current

def ucb1(node: MCTSNode, C: float) -> float:
    if node.visit_count == 0:
        return float("inf")
    exploitation = node.average_reward
    exploration = C * math.sqrt(math.log(node.parent.visit_count) / node.visit_count)
    return exploitation + exploration
```

#### 5.3.2 Expansion

```python
def expand(node: MCTSNode) -> MCTSNode:
    if not node.untried_actions:
        return node
    
    action = node.untried_actions.pop(0)  # or random / priority
    child = MCTSNode(
        node_id=f"{node.node_id}_{len(node.children)}",
        parent=node,
        action=action,
        path_history=node.path_history + [action],
        untried_actions=[]  # will be filled if needed, or inherit remaining
    )
    
    # Remaining actions for the child = parent's remaining minus used ones
    # (simple version: re-calculate available SubEntities not yet used)
    child.untried_actions = compute_remaining_actions(child.path_history, original_candidates, transforms)
    
    node.children.append(child)
    return child
```

#### 5.3.3 Simulation (Rollout)

```python
def simulate(
    node: MCTSNode,
    ground_truth: str,
    doc_graph: DocGraph,
    candidates: List[SubEntity],
    transforms: List[Transformation],
    max_depth: int
) -> float:
    # Copy the path so far
    path = list(node.path_history)
    depth = len(path)
    
    # Random / heuristic rollout until terminal
    while depth < max_depth and not is_terminal(path, ground_truth):
        remaining = compute_remaining_actions(path, candidates, transforms)
        if not remaining:
            break
        action = random.choice(remaining)
        path.append(action)
        depth += 1
    
    return calculate_reward(path, ground_truth, doc_graph)
```

#### 5.3.4 Backpropagation

```python
def backpropagate(node: MCTSNode, reward: float):
    current = node
    while current is not None:
        current.visit_count += 1
        current.total_reward += reward
        current = current.parent
```

### 5.4 Main Loop

```python
def run_mcts(
    doc_graph: DocGraph,
    candidates: List[SubEntity],
    ground_truth: str,
    document_id: str,
    entity_name: str,
    config: MCTSConfig = MCTSConfig()
) -> List[Pattern]:
    
    # 1. Warm-start with Beam Search
    warm_paths = beam_search_warmstart(
        candidates, TRANSFORM_LIBRARY, ground_truth,
        beam_width=config.beam_width_for_warmstart
    )
    
    # 2. Create root
    root = MCTSNode(node_id="root", path_history=[])
    root.untried_actions = list(candidates) + list(TRANSFORM_LIBRARY)
    
    # Seed root with warm-start children
    for wp in warm_paths:
        child = MCTSNode(
            node_id=f"warm_{len(root.children)}",
            parent=root,
            path_history=wp,
            action=wp[-1] if wp else None
        )
        root.children.append(child)
    
    # 3. Main MCTS iterations
    for i in range(config.iterations):
        node = select(root, config.exploration_constant)
        node = expand(node)
        reward = simulate(node, ground_truth, doc_graph, candidates, TRANSFORM_LIBRARY, config.max_depth)
        backpropagate(node, reward)
    
    # 4. Extract best paths
    best_raw_paths = extract_best_paths(root, min_reward=config.min_reward_to_keep, top_k=10)
    
    # 5. Lift to rich Patterns (Phase 6 wiring)
    patterns = []
    for path in best_raw_paths:
        pattern = lift_to_pattern(
            path=path,
            ground_truth=ground_truth,
            doc_graph=doc_graph,
            document_id=document_id,
            entity_name=entity_name
        )
        patterns.append(pattern)
    
    return sorted(patterns, key=lambda p: p.confidence, reverse=True)
```

---

## 6. Reward Function (Critical)

This is the single most important function. It must reflect the ultimate goal.

```python
def calculate_reward(
    path: List[Union[SubEntity, Transformation]],
    ground_truth: str,
    doc_graph: DocGraph
) -> float:
    reconstructed = reconstruct(path)          # apply all transforms in order
    
    # --- Surface match ---
    str_sim = normalized_levenshtein(reconstructed, ground_truth)
    token_cov = token_coverage(reconstructed, ground_truth)
    
    # --- Semantic ---
    sem_sim = cosine_similarity(
        embed(reconstructed), embed(ground_truth)
    )
    
    # --- Context coherence (uses the graph) ---
    ctx_score = 0.0
    used_sections = set()
    for item in path:
        if isinstance(item, SubEntity):
            used_sections.add(item.source_section_id)
            # Bonus if section_description supports the role
            if role_matches_description(item.role, item.section_description):
                ctx_score += 0.15
    ctx_score = min(ctx_score, 1.0)
    
    # --- Structural clarity ---
    n_components = sum(1 for x in path if isinstance(x, SubEntity))
    clarity = 1.0 / (1.0 + max(0, n_components - 2))   # prefer 1–3 components
    
    # --- Simplicity ---
    n_transforms = sum(1 for x in path if isinstance(x, Transformation))
    simplicity = 1.0 / (1.0 + n_transforms * 0.3)
    
    # Weighted combination
    reward = (
        0.30 * str_sim +
        0.15 * token_cov +
        0.20 * sem_sim +
        0.15 * ctx_score +
        0.10 * clarity +
        0.10 * simplicity
    )
    
    # Perfect match bonus
    if reconstructed.strip() == ground_truth.strip():
        reward = min(1.0, reward + 0.15)
    
    return reward
```

---

## 7. LLM Integration Points (Intelligent Reasoning)

The core MCTS loop stays deterministic for speed and controllability.  
LLM is injected only at high-value decision points using structured ADK-style calls (clear input schema + `output_key`).

### 7.1 Smart Rollout Policy (Simulation)

**When:** During the Simulation phase, instead of pure random action selection.

**Goal:** Let the LLM propose the most promising next action given the current partial path.

**Input:**
```json
{
  "partial_path": [
    {"type": "SubEntity", "role": "administrator", "raw_value": "Citco", "section_description": "..."},
    {"type": "Transformation", "name": "lower"}
  ],
  "remaining_candidates": [
    {"role": "risk_level", "raw_value": "high risk", "section_description": "..."},
    {"role": "risk_level", "raw_value": "High Risk", "section_description": "..."}
  ],
  "available_transforms": ["concat_hyphen_space", "concat_hyphen", "lower"],
  "ground_truth": "cITCO- high risk",
  "entity_name": "risk-type"
}
```

**Prompt:**
```
You are an expert at reverse-engineering how structured values are derived from legal credit agreements.

Current partial derivation path:
{partial_path}

Remaining candidate sub-entities:
{remaining_candidates}

Available transformations:
{available_transforms}

Target ground truth: "{ground_truth}"
Entity: {entity_name}

Task: Propose the single most promising next action (either one SubEntity or one Transformation) that is most likely to lead to the ground truth.

Respond with a JSON object only.
```

**Output (ADK style):**
```json
{
  "output_key": "next_action",
  "action_type": "SubEntity" | "Transformation",
  "action_id_or_name": "risk_level::high risk" | "concat_hyphen_space",
  "reasoning": "Adding the risk_level 'high risk' after normalizing the administrator name is the most direct way to form the target string.",
  "confidence": 0.87
}
```

**How it is used:**  
In `simulate()`, with probability `p_llm` (e.g. 0.7) call the LLM; otherwise fall back to random.  
This gives intelligent guidance without making every iteration slow.

---

### 7.2 Intention & Context Coherence Scorer (Reward Augmentation)

**When:** After the mathematical reward is calculated, on promising paths (or every N simulations).

**Goal:** Judge whether the path makes real semantic and contextual sense.

**Input:**
```json
{
  "path": [ ... full path objects ... ],
  "reconstructed_value": "cITCO- high risk",
  "ground_truth": "cITCO- high risk",
  "section_descriptions": {
    "sec_4_1_2": "Appoints Citco as the fund administrator...",
    "sec_7_3": "Defines the internal risk category of the Borrower as High Risk"
  }
}
```

**Prompt:**
```
You are evaluating whether a derivation path correctly and sensibly explains a ground-truth value extracted from a credit agreement.

Ground truth: "{ground_truth}"
Reconstructed value: "{reconstructed_value}"

Path components and their source descriptions:
{path_with_descriptions}

Score from 0.0 to 1.0 how well this path explains the ground truth, considering:
- Do the source sections actually support the roles assigned?
- Is the combination logical?
- Does the final value match the intention of a "risk-type"?

Respond with JSON only.
```

**Output (ADK style):**
```json
{
  "output_key": "intention_score",
  "score": 0.92,
  "reasoning": "The administrator name comes from the correct appointment section and the risk level from the classification section. The normalization to 'cITCO' matches internal convention. Highly coherent.",
  "role_support": {
    "administrator": true,
    "risk_level": true
  }
}
```

**How it is used:**  
Final reward = `0.75 * mathematical_reward + 0.25 * intention_score`

---

### 7.3 Intention Summary Generator (Pattern Lifting)

**When:** Only on the top-K paths after MCTS finishes (Phase 6).

**Goal:** Produce a clear, human-readable explanation of *why* this path produces the ground truth.

**Input:**
```json
{
  "components": [
    {
      "role": "administrator",
      "raw_value": "Citco",
      "normalized_value": "cITCO",
      "section_title": "Appointment of Administrator",
      "section_description": "..."
    },
    {
      "role": "risk_level",
      "raw_value": "high risk",
      "section_title": "Risk Classification",
      "section_description": "..."
    }
  ],
  "transforms_applied": ["lower", "concat_hyphen_space"],
  "reconstructed_value": "cITCO- high risk",
  "ground_truth": "cITCO- high risk",
  "entity_name": "risk-type"
}
```

**Prompt:**
```
You are writing a concise explanation of how a structured value was derived from a credit agreement.

Entity: {entity_name}
Ground truth: "{ground_truth}"
Reconstructed: "{reconstructed_value}"

Components used:
{components}

Transforms applied:
{transforms_applied}

Write a clear 2–4 sentence intention_summary that explains:
1. Which pieces were taken from which parts of the document
2. What transformations were applied
3. Why this combination correctly produces the ground-truth value

Respond with JSON only.
```

**Output (ADK style):**
```json
{
  "output_key": "intention_summary",
  "summary": "The value is formed by taking the fund administrator name 'Citco' from the Appointment section, normalizing it to the internal convention 'cITCO', and appending the risk classification 'high risk' found in the Risk Classification section, joined by a hyphen-space. This matches the observed ground-truth format for risk-type.",
  "confidence": 0.91
}
```

---

### 7.4 Optional: Action Ranking during Expansion

**When:** When a node has many untried actions (> 8).

**Goal:** Let LLM prioritize which candidates to try first.

**Input / Output:** Same style as Smart Rollout, but returns a ranked list:

```json
{
  "output_key": "ranked_actions",
  "actions": [
    {"action_type": "SubEntity", "action_id_or_name": "...", "score": 0.91},
    {"action_type": "Transformation", "action_id_or_name": "...", "score": 0.74}
  ]
}
```

---

### 7.5 Design Rules for LLM Calls

- Always use structured JSON output with a clear `output_key`.
- Keep prompts focused and short.
- Cache LLM results for identical partial paths (important for speed).
- Fall back to deterministic behaviour if the LLM call fails or times out.
- Log every LLM decision for full auditability.
- Never let the LLM directly modify the MCTS tree statistics — it only proposes actions or scores.

---

## 8. Reconstruction Helper

```python
def reconstruct(path: List[Union[SubEntity, Transformation]]) -> str:
    """
    Walk the path and apply SubEntities + Transformations in order.
    Returns the final string.
    """
    current = ""
    value_stack = []
    
    for item in path:
        if isinstance(item, SubEntity):
            val = item.normalized_value or item.raw_value
            value_stack.append(val)
            current = val if not current else current  # simple version
        elif isinstance(item, Transformation):
            if item.is_binary and len(value_stack) >= 2:
                b = value_stack.pop()
                a = value_stack.pop()
                result = item.apply(a, b)
                value_stack.append(result)
                current = result
            else:
                # Unary
                if value_stack:
                    a = value_stack.pop()
                    result = item.apply(a, None)
                    value_stack.append(result)
                    current = result
    return current if value_stack else current
```

---

## 8. Pattern Lifting (Wiring to Phase 6)

```python
def lift_to_pattern(
    path: List[Union[SubEntity, Transformation]],
    ground_truth: str,
    doc_graph: DocGraph,
    document_id: str,
    entity_name: str
) -> Pattern:
    
    components = [x for x in path if isinstance(x, SubEntity)]
    transforms = [x for x in path if isinstance(x, Transformation)]
    
    reconstructed = reconstruct(path)
    confidence = calculate_reward(path, ground_truth, doc_graph)
    
    # Build human-readable structure
    structure_parts = []
    for item in path:
        if isinstance(item, SubEntity):
            structure_parts.append(f"{item.role}({item.raw_value})")
        else:
            structure_parts.append(item.name)
    structure = " → ".join(structure_parts)
    
    # Intention summary — generated by LLM (see Section 7.3)
    intention = generate_intention_summary_via_llm(
        components=components,
        transforms=transforms,
        reconstructed_value=reconstructed,
        ground_truth=ground_truth,
        entity_name=entity_name
    )
    
    return Pattern(
        pattern_id=f"pat_{uuid.uuid4().hex[:8]}",
        document_id=document_id,
        target_entity=entity_name,
        reconstructed_value=reconstructed,
        confidence=confidence,
        structure=structure,
        components=components,
        transforms_applied=transforms,
        intention_summary=intention,
        source_section_ids=[c.source_section_id for c in components]
    )
```

---

## 9. Integration Contract with Previous Phases

```python
# From Phase 3 (Nearest-Neighbour)
candidates: List[SubEntity] = nearest_neighbour_retrieve(
    doc_graph=doc_graph,
    entity_name=entity_name,
    ground_truth=ground_truth,
    top_k=30
)

# Call MCTS
patterns = run_mcts(
    doc_graph=doc_graph,
    candidates=candidates,
    ground_truth=ground_truth,
    document_id=document_id,
    entity_name=entity_name
)

# Pass to Phase 5 / 6
evidence_package = build_evidence_package(patterns, doc_graph)
# ... then Pattern Lifting is already done inside run_mcts
```

**Hard rules:**
- MCTS may **only** use SubEntities that came from Phase 3.
- Every SubEntity carries its `source_section_id` and `section_description` — these must survive into the final Pattern.
- No information from other documents is visible.

---

## 10. Why This Design is State-of-the-Art for the Problem

| Decision | Reason |
|----------|--------|
| Beam → MCTS two-stage | Beam quickly finds strong short paths; MCTS explores deeper / non-obvious combinations |
| UCB1 selection | Proven balance of exploration vs exploitation |
| Path history stored on every node | Allows perfect reconstruction and full provenance |
| Rich reward (context + structure + intention proxies) | Pure string match is insufficient for our goal |
| Explicit Pattern lifting | Turns a search path into the reusable artifact the overall system needs |
| Strict candidate pool | Prevents noise from the full document; keeps search focused |

---

## 11. Implementation Checklist

- [ ] Data models (`SubEntity`, `Transformation`, `MCTSNode`, `Pattern`)
- [ ] Transformation library (at least the 6 base transforms)
- [ ] `reconstruct()` helper
- [ ] Full `calculate_reward()` with all six components
- [ ] Beam Search warm-start
- [ ] MCTS four phases + main loop
- [ ] LLM Smart Rollout policy (Section 7.1)
- [ ] LLM Intention & Context Scorer (Section 7.2)
- [ ] LLM Intention Summary Generator (Section 7.3)
- [ ] `lift_to_pattern()` wired to LLM summary
- [ ] Caching layer for LLM calls
- [ ] Unit tests with synthetic paths that must recover `cITCO- high risk`
- [ ] Integration test: Phase 3 candidates → MCTS → Pattern
- [ ] Logging of tree statistics + LLM decisions

---

## 12. Next Immediate Coding Steps

1. Create `mcts/` package with the data models.
2. Implement pure functions: `reconstruct`, `calculate_reward`, `ucb1`.
3. Implement Beam Search warm-start.
4. Implement the four MCTS phases (deterministic first).
5. Add LLM injection points (Smart Rollout + Intention Scorer + Summary).
6. Wire `run_mcts` to accept real Phase-3 candidates.
7. Add a small synthetic test that injects two SubEntities ("Citco", "high risk") and verifies the system can recover the ground truth via the correct transforms.

---

*Document version: 1.1*  
*Updated: 2026-08-25 – Added full LLM Integration Points (Section 7) with ADK-style prompts, inputs, and output_key schemas*  
*This document is the definitive specification for the MCTS core.  
It is fully wired to Phases 0–3 (inputs) and Phases 5–6 (outputs) of the overall reverse-engineering pipeline.*
