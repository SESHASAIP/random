# End-to-End Implementation Plan  
## Reverse-Engineering Entity Extraction from Wholesale Credit Agreements

**Target Example**  
- Entity: `risk-type`  
- Ground Truth: `cITCO- high risk`  

**Goal**  
Build a controllable, hybrid (deterministic + LLM) system that:  
1. Ingests a full credit agreement `.txt`  
2. Performs a linear section-by-section analysis  
3. Builds a traversable graph/tree of the document  
4. Uses the target entity + ground-truth value to retrieve relevant signals  
5. Explores and scores combinations of sub-level entities  
6. Reconstructs the most probable derivation path that produced the ground-truth value  

This plan follows the exact human process you described and turns it into a production-ready, agentic pipeline.

---

## 1. High-Level Architecture

```
┌─────────────────┐
│  Credit Agreement│
│      .txt        │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Linear Pass +   │
│  Section Analysis│
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Graph / Tree    │
│  Construction    │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Entity + GT     │
│  Nearest-Neighbour│
│  Retrieval       │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Sub-Entity      │
│  Candidate Pool  │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Combination     │
│  Exploration &   │
│  Scoring         │
│  (MCTS)          │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Ranked Derivation│
│  Paths + Evidence │
└─────────────────┘
```

---

## 2. Detailed End-to-End Flow

### Phase 0 – Input Preparation
- Input: full document as plain text (`.txt`)
- Optional metadata: document ID, bank, facility type, date
- Normalize: remove excessive whitespace, preserve original headings and paragraph boundaries

### Phase 1 – Linear Section-by-Section Pass
**Objective**: Walk the document from first heading to last paragraph and produce a short analysis for every meaningful unit.

**Steps**:
1. Detect hierarchical structure (Heading 1 → Heading 2 → … → paragraphs)
2. For each section / subsection:
   - Extract title + full text span
   - Generate a short analysis note (3–6 sentences) that answers:
     - What is this section about?
     - Does it contain any risk-related language, administrator names, classifications, or defined terms?
     - Any potential sub-entities that could later contribute to a composite value?
3. Attach metadata to each unit:
   - `section_id`
   - `level` (1, 2, 3…)
   - `start_char` / `end_char`
   - `short_analysis`
   - `raw_text`
   - `preliminary_signals` (list of candidate phrases)

**Implementation note**:  
Use a hybrid approach — deterministic heading detection + LLM for the short analysis note (with a strict system prompt that forces concise, factual output).

### Phase 2 – Graph / Tree Construction
**Objective**: Turn the linear analysis into a traversable structure.

**Recommended structure**:
- **Primary**: Tree (parent–child hierarchy mirrors document outline)
- **Secondary**: Lightweight graph edges for cross-references (defined terms, “see Schedule X”, “as defined in Section Y”)

**Node schema**:
```json
{
  "id": "sec_3_2_1",
  "title": "Risk Classification",
  "level": 3,
  "parent_id": "sec_3_2",
  "short_analysis": "...",
  "raw_text": "...",
  "signals": ["high risk", "Citco", ...],
  "embedding": [0.12, -0.45, ...],
  "relevance_score": null
}
```

**Storage options**:
- In-memory NetworkX / igraph for prototyping
- Postgres + recursive CTEs or Apache AGE / Neo4j for production
- Vector index (pgvector / FAISS) on the `embedding` field for nearest-neighbour search

### Phase 3 – Entity + Ground-Truth Nearest-Neighbour Retrieval
**Inputs**:
- Target entity name (`risk-type`)
- Ground-truth value (`cITCO- high risk`)

**Process**:
1. Embed the query: `"risk-type" + "cITCO- high risk"` (or separate embeddings + weighted combination)
2. Retrieve top-k nodes from the graph using:
   - Semantic similarity (embedding cosine)
   - Keyword overlap (BM25 or simple token match)
   - Hierarchical boost (prefer nodes closer to risk-related parent sections)
3. Rank and prune nodes that clearly cannot contribute
4. Extract all candidate **sub-level entities** from the surviving nodes

**Output of this phase**:  
A ranked list of candidate signals, each carrying:
- Source node ID
- Extracted text span
- Confidence / similarity score
- Suggested role (e.g. “administrator”, “risk_level”, “prefix”, “connector”)

### Phase 4 – Combination Exploration & Scoring
**Algorithm of choice**: **Monte Carlo Tree Search (MCTS)**

**Why MCTS**:
- Excellent balance of exploration vs exploitation when the space of signal combinations + transforms grows
- Naturally handles variable-depth paths and optional transformations
- Produces a search tree that can be inspected (aligns with explicit, controllable design)
- Reward can be the same multi-objective score used for ranking final paths
- Works well as a LangGraph node or hybrid agent component
- Scales better than pure beam search when many candidate signals or transform variants exist

**Scoring / Reward function (multi-objective)**:
```
reward = w1 * string_similarity(combined, ground_truth)
       + w2 * token_coverage(combined, ground_truth)
       + w3 * semantic_similarity(combined, ground_truth)
       + w4 * source_quality (average node relevance)
       - w5 * complexity_penalty (number of signals + transform complexity)
```

**Transforms allowed inside the search**:
- Case normalization (`CITCO` → `cITCO`)
- Concatenation patterns (`A + "-" + B`, `A + "- " + B`, etc.)
- Light cleaning (remove extra spaces, standardize hyphens)
- Optional LLM-proposed transforms (only when deterministic rules fail)

**MCTS loop (classic 4 steps)**:
1. **Selection**  
   Starting from the root (empty combination), traverse the tree using UCB1 (or a variant) to select the most promising leaf that still has unexplored actions.

2. **Expansion**  
   From the selected node, add one or more child nodes by applying a remaining signal + optional transform.

3. **Simulation (Rollout)**  
   From the new child, perform a lightweight rollout (random or heuristic completion of the combination) until a terminal state (max signals reached or no useful signals left). Evaluate the final combination with the reward function above.

4. **Backpropagation**  
   Propagate the reward back up the path, updating visit counts and average values for every node on the path.

**Termination & Output**:
- Run a fixed number of iterations (e.g. 500–5000 depending on signal count) or until convergence
- After search, extract the highest-value paths from the tree
- Return ranked final combinations with full provenance (signals used, transforms, source sections, visit statistics)

### Phase 5 – Output & Evidence Package
For every high-scoring combination produce:
- Final reconstructed string
- List of sub-entities used (with source sections)
- Exact transforms applied
- Overall score + breakdown
- Human-readable derivation narrative
- Confidence that this path explains the ground truth

---

## 3. Flow Diagram (Mermaid)

```mermaid
flowchart TD
    A[Credit Agreement .txt] --> B[Phase 1: Linear Section Pass]
    B --> B1[Detect Headings & Paragraphs]
    B1 --> B2[Generate Short Analysis per Section]
    B2 --> B3[Extract Preliminary Signals]
    B3 --> C[Phase 2: Graph/Tree Construction]
    C --> C1[Build Hierarchy Tree]
    C1 --> C2[Add Cross-Reference Edges]
    C2 --> C3[Compute Embeddings]
    C3 --> D[Phase 3: Entity + GT Retrieval]
    D --> D1[Embed Query: entity + ground_truth]
    D1 --> D2[Nearest-Neighbour + Keyword Retrieval]
    D2 --> D3[Prune Irrelevant Nodes]
    D3 --> D4[Extract Sub-Entity Candidates]
    D4 --> E[Phase 4: Combination Search - MCTS]
    E --> E1[Selection UCB1]
    E1 --> E2[Expansion: add signal + transform]
    E2 --> E3[Simulation / Rollout]
    E3 --> E4[Backpropagation of Reward]
    E4 --> E5{Iterations complete?}
    E5 -->|No| E1
    E5 -->|Yes| E6[Extract Ranked Paths from Tree]
    E6 --> F[Phase 5: Evidence Package]
    F --> F1[Ranked Derivation Paths]
    F1 --> F2[Sub-Entities + Source Sections]
    F2 --> F3[Transforms Applied]
    F3 --> F4[Human-Readable Explanation]
```

---

## 4. Component Design (Hybrid Agent Friendly)

| Component                    | Type              | Technology Suggestion                  | Notes |
|-----------------------------|-------------------|----------------------------------------|-------|
| Section Splitter            | Deterministic     | Regex + heading heuristics             | Fast, reliable |
| Short Analysis Generator    | LLM               | Structured output / JSON mode          | Strict prompt |
| Graph Builder               | Deterministic     | NetworkX / custom tree                 | Easy to serialize |
| Embedding & Retrieval       | Hybrid            | pgvector + BM25                        | Production ready |
| MCTS Engine                 | Deterministic     | Pure Python / LangGraph node           | Fully controllable, exploration-aware |
| Transform Proposer          | Optional LLM      | Only when needed                       | Keep hybrid |
| Scoring Function            | Deterministic     | Weighted multi-objective               | Tunable |
| Orchestrator                | State Machine     | LangGraph                              | Matches your preference |

---

## 5. Data Contracts

**Sub-Entity Candidate**
```json
{
  "id": "sig_017",
  "text": "Citco",
  "normalized": "cITCO",
  "role_hypothesis": ["administrator", "prefix"],
  "source_node": "sec_4_1_2",
  "similarity_to_gt": 0.71,
  "span": [12450, 12455]
}
```

**Combination Result**
```json
{
  "rank": 1,
  "reconstructed": "cITCO- high risk",
  "score": 0.94,
  "sub_entities": ["sig_017", "sig_023"],
  "transforms": ["case_normalize(Citco → cITCO)", "concat(A + '- ' + B)"],
  "provenance": ["sec_4_1_2", "sec_7_3"],
  "explanation": "Administrator name from Section 4.1.2 normalized to cITCO + risk classification 'high risk' from Section 7.3 concatenated with hyphen-space."
}
```

---

## 6. Implementation Roadmap

**Sprint 1 – Foundation (1–2 weeks)**  
- Section splitter + short analysis generator  
- Basic tree construction  
- Simple embedding + retrieval  

**Sprint 2 – Core Search (1–2 weeks)**  
- Candidate extraction  
- MCTS engine + multi-objective reward function  
- Transform library (case, concat, clean)  
- UCB1 selection + rollout policy  

**Sprint 3 – Hybrid & Production (1–2 weeks)**  
- LangGraph orchestration  
- Cross-reference graph edges  
- Evidence package generation  
- Evaluation harness (compare reconstructed vs ground truth)  

**Sprint 4 – Hardening**  
- Caching, versioning of graphs  
- Human-in-the-loop review UI  
- Batch processing of multiple agreements  

---

## 7. Evaluation Metrics

- Exact match rate of top-1 reconstructed string vs ground truth  
- Token-level F1 of the combination  
- Rank of the correct derivation path  
- Average number of signals needed  
- Human judgment of explanation quality  

---

## 8. Key Design Principles (Aligned with Your Preferences)

- Explicit control flow (state machine / LangGraph) over fully dynamic loops  
- Hybrid: deterministic core + LLM only where judgment is required  
- Every intermediate result is inspectable and attributable to source sections  
- Graph remains the single source of truth after the linear pass  
- Combination search is scored against the high-level goal at every step  

---

## 9. Next Immediate Actions

1. Supply a real credit agreement `.txt` (or a representative excerpt)  
2. Run Phase 1–2 on it and inspect the generated graph  
3. Execute Phase 3–4 for the `risk-type` / `cITCO- high risk` example  
4. Iterate on scoring weights and allowed transforms  

---

*Document version: 1.1*  
*Updated: 2026-08-24 – Replaced Beam Search with Monte Carlo Tree Search (MCTS)*  
*Aligned with human reverse-engineering process + hybrid agentic architecture*
