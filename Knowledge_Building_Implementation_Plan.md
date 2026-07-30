# Self-Bootstrapping Reflective Knowledge Construction System
## Detailed Implementation Plan
**Version:** 1.0  
**Date:** 2026-07-29  
**Purpose:** Open Entity Discovery + On-the-fly Knowledge Building from Financial Documents  
**Core Constraint:** Starts with zero external knowledge base / no pre-defined schema / no external RAG

---

## 1. System Goal

Build a high-precision, auditable set of entities from a single financial document (trade agreements, BPO, fraud ops agreements, etc.) by simulating how a junior financial analyst would read and understand the document for the first time.

The system must:
- Discover entities without a fixed schema
- Build knowledge from the document itself
- Use only the LLM’s pre-trained financial/legal knowledge at the start
- Improve through self-bootstrapping (episodic memory)
- Produce stable, well-defined, source-grounded entities

---

## 2. High-Level Architecture

**Pattern Name:** Self-Bootstrapping Reflective Knowledge Construction Loop  
**+ Discussion Team at Core Knowledge Building Stages**

### 2.1 Overall System Architecture (How it fits into DocBrain)

This diagram shows how the new Knowledge Building Core integrates with the existing DocBrain Entity Discovery flow.

```
┌──────────────────────────────────────────────────────────────┐
│                    EXISTING DOCBRAIN FLOW                    │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ECSP OCR (PDF / DOCX / Text)                                │
│           ↓                                                  │
│  core/document_extractor.py                                  │
│           ↓                                                  │
│  OCRed corpus (grouped by doctype)                           │
│           ↓                                                  │
│  ┌─────────────────────────┐    ┌─────────────────────────┐ │
│  │ build_doctype_knowledge │ →  │ Doctype Knowledge Pack  │ │
│  │ (ontology + taxonomy)   │    │ overview.md             │ │
│  └─────────────────────────┘    │ glossary.md             │ │
│                                 │ taxonomy.md             │ │
│                                 │ Entity Taxonomy Mapping │ │
│                                 └─────────────────────────┘ │
│                                           ↓                  │
│                                 Markdown Wiki per document   │
│                                 (build_llm_wiki.py)          │
│                                           ↓                  │
└──────────────────────────────────────────────────────────────┘
                                            ↓
┌──────────────────────────────────────────────────────────────┐
│              NEW KNOWLEDGE BUILDING CORE                     │
│         (Replaces extract_entities_from_wiki_file.py)        │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. Initial Discovery Agent                                  │
│     - Strong reasoning model                                 │
│     - Chain-of-Thought forcing pre-trained knowledge         │
│     - High recall candidate generation                       │
│                                                              │
│  2. Concept Formation + Discussion Team                      │
│     - Analyst  → proposes concepts                           │
│     - Skeptic  → challenges proposals                        │
│     - Moderator → forces clean decisions                     │
│                                                              │
│  3. Critic / Consistency Agent                               │
│     - Source grounding check                                 │
│     - Internal consistency                                   │
│     - Domain rule validation                                 │
│                                                              │
│  4. Episodic Memory Writer + Refinement Loop (max 3)         │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                                            ↓
┌──────────────────────────────────────────────────────────────┐
│                    FINAL OUTPUT                              │
│  Clean Entity JSON + SQLite entities table                   │
│  (Ready for Wiki Chat UI)                                    │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Core Pipeline Stages (Knowledge Building Only)

1. **Document Parser Agent**
2. **Initial Discovery Agent** (Invention Stage)
3. **Concept Formation + Discussion Team** (Analyst – Skeptic – Moderator)
4. **Critic / Consistency Agent + Second Discussion**
5. **Episodic Memory Writer**
6. **Refinement Loop** (max 3 iterations)

---

## 3. Detailed Stage Design

### 3.1 Document Parser Agent

**Responsibility:**  
Prepare clean, structured representations of the document so downstream agents can reason accurately.

**Inputs:**  
- Raw OCR text (or layout-aware parsed text)

**Outputs:**
- Clean plain text
- Structured markdown (with heading hierarchy)
- Table-aware JSON (headers + rows)
- Document fingerprint (type signals, length, presence of exhibits/schedules)

**Key Requirements:**
- Preserve structural hierarchy
- Flag low-quality OCR segments
- Never drop content — only restructure it

**Implementation Notes:**
- Can use LlamaParse, Unstructured, or custom layout-aware parser
- Output should be passed as structured state to the next stage

---

### 3.2 Initial Discovery Agent (Critical Invention Stage)

**Responsibility:**  
Surface as many candidate entities as possible using only the LLM’s pre-trained financial knowledge.

**Goal:** High Recall

**Model Recommendation:**  
Use the strongest available reasoning model (Claude 4 Opus, OpenAI o3/o4, or equivalent).  
Do **not** use a weak or fast model here.

**Chain-of-Thought Forcing Structure (Mandatory):**

1. Domain priming (“You are a junior financial analyst who has read hundreds of trade agreements, BPO contracts...”)
2. Significance test against pre-trained knowledge
3. Classification + justification
4. Confidence + ambiguity declaration

**Output Schema (per candidate):**
```json
{
  "span": "exact text quote",
  "classification": "Term | Obligation | Party Role | Condition | Definition | Other",
  "justification": "why this is significant based on financial/legal knowledge",
  "confidence": 0.0-1.0,
  "ambiguity": "optional note"
}
```

**Rules:**
- Temperature: 0.3 – 0.5 (allow some exploration)
- Must not invent entities not grounded in the text
- Encouraged to surface borderline candidates with lower confidence
- This is the **only** stage allowed to freely invent new candidates (besides refinement re-runs)

---

### 3.3 Concept Formation + Discussion Team

**This is the core knowledge-building stage.**

**Team Composition:**

| Role       | Responsibility                                      | Behavior Style      |
|------------|-----------------------------------------------------|---------------------|
| Analyst    | Proposes clusters + draft definitions               | Constructive        |
| Skeptic    | Challenges proposals, points out weaknesses         | Adversarial         |
| Moderator  | Forces resolution and synthesizes final version     | Decisive           |

**Discussion Protocol:**

1. Analyst receives candidates from Step 2 and proposes concept clusters + definitions.
2. Skeptic attacks each proposal (weak grounding, over-generalization, possible merges, alternative interpretations).
3. Analyst responds with refinements or counter-evidence.
4. Moderator synthesizes the strongest version and assigns a discussion confidence score.
5. Only concepts that survive the full discussion advance.

**Control Rules:**
- Maximum 2 discussion rounds per concept
- Moderator has final authority
- Full discussion transcript must be logged for auditability

**Output:**  
Stabilized concepts with:
- Canonical name
- Clear definition
- Inclusion / exclusion criteria
- Supporting evidence spans
- Discussion confidence

---

### 3.4 Critic / Consistency Agent + Second Discussion

**Responsibility:**  
Hard consistency checking + second verification layer.

**Checks:**
- Source grounding (must have direct quote)
- Internal consistency (same concept defined differently in different places)
- Domain rules (e.g., an Obligation should have clear obligor/obligee)
- Confidence decay on failed checks

**Behavior:**
- If a concept fails or is borderline → re-activate the Discussion Team on that specific item
- Maintain a Rejection Log with reasons

---

### 3.5 Episodic Memory Writer

**Responsibility:**  
Store only the knowledge that has survived both discussion rounds.

**Stored Fields per Entity:**
- Canonical name
- Stable definition
- Supporting evidence spans (with location references)
- Confidence trajectory across iterations
- Version number

**Technical Requirements:**
- Embedding-based deduplication
- Versioning support
- Acts as the **only** knowledge source for subsequent refinement iterations (true self-bootstrapping)

---

### 3.6 Refinement Loop

**Rules:**
- Maximum 3 full iterations
- Early stopping conditions:
  - No new entities proposed
  - Average confidence of the knowledge set above threshold
  - Critic finds zero unresolved contradictions
- On final iteration → force a global consistency sweep across the entire document

**Important:**  
During refinement, Step 2 (Initial Discovery) is re-run with access to the current episodic memory. This is how missed entities are recovered.

---

## 4. State Schema (Recommended)

```python
class KnowledgeState(TypedDict):
    raw_document: str
    parsed_document: dict          # structured representations
    candidates: List[dict]         # from Initial Discovery
    concepts: List[dict]           # after Discussion Team
    episodic_memory: List[dict]    # accepted entities
    rejection_log: List[dict]
    discussion_transcripts: List[dict]
    iteration: int
    final_entities: List[dict]
    confidence_report: dict
```

---

## 5. Model Assignment Strategy

| Stage                        | Recommended Model Tier          | Reason |
|-----------------------------|-------------------------------|--------|
| Document Parser             | Fast / specialized parser     | Not LLM-heavy |
| Initial Discovery (Step 2)  | Strongest Reasoning Model     | Critical invention stage |
| Discussion Team             | Strong Reasoning Model        | High quality debate |
| Critic                      | Strong Reasoning Model        | Consistency is critical |
| Episodic Memory operations  | Lightweight + embeddings      | Cost control |

**Principle:**  
Give the strongest reasoning model to Step 2 and the Discussion Team. These are the highest-leverage stages.

---

## 6. Output Artifacts

At the end of the process the system must produce:

1. **Final Entity Set**  
   - Canonical name  
   - Definition  
   - Evidence spans  
   - Final confidence

2. **Confidence Report**

3. **Rejection Log**  
   - What was considered and why it was discarded

4. **Discussion Transcripts** (optional but recommended for audit)

---

## 7. Implementation Recommendations

- **Orchestration:** LangGraph (state machine + cycles) is the best fit for the Refinement Loop and Discussion rounds.
- **Structured Output:** Force JSON schema on every agent.
- **Temperature Strategy:**
  - Step 2: 0.3–0.5
  - Discussion & Critic: 0.1–0.3
- **Observability:** Log every agent decision and discussion turn.
- **Human-in-the-Loop Hook:** Flag entities with final confidence between 0.65–0.85 for optional review.

---

## 8. Success Criteria

The system is considered successful when:

- Entities are source-grounded
- Definitions are stable across iterations
- Coverage is high (few important entities missed)
- False positives are low
- The knowledge set improves meaningfully between iteration 1 and iteration 3
- Full audit trail exists for every accepted entity

---

## 9. Next Implementation Steps

1. Define exact Pydantic / JSON schemas for all agent outputs
2. Write production-grade prompts for:
   - Initial Discovery Agent (CoT)
   - Analyst / Skeptic / Moderator
   - Critic
3. Implement the LangGraph control flow
4. Build the episodic memory store (with embeddings + versioning)
5. Create evaluation harness (measure recall, precision, definition stability)

---

---

## 10. Prompt Library (Full Example Prompts)

Below are production-ready example prompts for each core agent.  
These prompts are designed to be used directly (with minor adaptation for your exact state schema).

---

### 10.1 Initial Discovery Agent Prompt (Step 2 – Invention Stage)

```text
You are a junior financial analyst who has read hundreds of trade agreements, BPO contracts, fraud operations agreements, and similar financial documents.

Your only task is to discover candidate entities from the provided text. 
You must rely on your pre-trained knowledge of financial and legal language.

Do NOT invent entities that are not grounded in the text.
Do NOT finalize definitions — only surface candidates.

Follow this exact reasoning process for every candidate:

1. Quote the exact text span.
2. Ask yourself: “Is this legally or financially significant based on my knowledge of contracts?”
3. Classify it into one of these categories using your pre-trained knowledge:
   - Contractual Term
   - Party Obligation
   - Party Role
   - Condition / Trigger
   - Definition / Interpretation
   - Other
4. Justify the classification by referring to typical contract language or structure.
5. Assign a confidence score between 0.0 and 1.0.
6. Note any ambiguity or alternative interpretation.

Output ONLY a valid JSON array. Each object must contain:
- span
- classification
- justification
- confidence
- ambiguity (optional)

Text to analyze:
{chunk}
```

---

### 10.2 Analyst Prompt (Discussion Team)

```text
You are the Analyst in a knowledge-building discussion team.

Your role is constructive. You propose coherent concepts from the candidate entities.

You will receive a list of raw candidates discovered from a financial document.

Your job:
1. Group related candidates into meaningful concepts.
2. Propose a clear canonical name for each concept.
3. Write a precise definition.
4. List the supporting evidence spans.
5. Explain why this concept is important in a financial/legal context.

Be concrete and grounded. Do not over-generalize.

Output your proposals in this JSON format:
{
  "proposed_concepts": [
    {
      "canonical_name": "",
      "definition": "",
      "supporting_spans": [],
      "reasoning": ""
    }
  ]
}
```

---

### 10.3 Skeptic Prompt (Discussion Team)

```text
You are the Skeptic in a knowledge-building discussion team.

Your role is adversarial and rigorous. Your job is to challenge the Analyst’s proposals.

For every proposed concept you must examine:
- Is the definition precise enough?
- Is the concept clearly grounded in the source text?
- Could this concept be merged with another?
- Are there alternative interpretations a careful reader might have?
- Is the Analyst over-claiming or under-claiming significance?

Be direct. Point out weaknesses clearly.
Do not accept vague or weakly supported concepts.

Output your critique in this JSON format:
{
  "critiques": [
    {
      "concept_name": "",
      "issues": [],
      "suggested_action": "refine | merge | reject | accept_with_changes",
      "alternative_interpretation": ""
    }
  ]
}
```

---

### 10.4 Moderator Prompt (Discussion Team)

```text
You are the Moderator in a knowledge-building discussion team.

You have received:
- The Analyst’s proposed concepts
- The Skeptic’s critiques

Your job is to force a clear decision on each concept.

For every concept:
1. Weigh the Analyst’s proposal against the Skeptic’s objections.
2. Decide the final version of the concept (or reject it).
3. Write a clean, stable definition.
4. Assign a final discussion confidence score (0.0–1.0).
5. Briefly explain your decision.

You have final authority. Prefer precision over quantity.

Output in this JSON format:
{
  "final_concepts": [
    {
      "canonical_name": "",
      "definition": "",
      "supporting_spans": [],
      "confidence": 0.0,
      "decision_reason": ""
    }
  ],
  "rejected_concepts": [
    {
      "name": "",
      "reason": ""
    }
  ]
}
```

---

### 10.5 Critic Prompt

```text
You are the Critic Agent responsible for final consistency and quality control.

You will receive a set of concepts that have already passed team discussion.

Your job is to perform hard checks:

1. Source Grounding: Does every concept have clear supporting text?
2. Internal Consistency: Are there conflicting definitions for the same idea?
3. Domain Rules: Does the concept make sense in a financial/legal context?
   - An Obligation should normally have a clear obligor and obligee.
   - A Term should have identifiable scope or conditions.
4. Confidence Calibration: Lower the confidence of any weak concept.

For each concept, decide:
- Accept
- Send back for discussion
- Reject

Output in this JSON format:
{
  "accepted": [],
  "send_back_for_discussion": [],
  "rejected": [
    {
      "concept_name": "",
      "reason": ""
    }
  ]
}
```

---

### 10.6 Usage Notes

- Always force structured JSON output (use response_format or equivalent).
- Keep temperature low for Analyst, Skeptic, Moderator, and Critic (0.1–0.3).
- Use higher temperature only for the Initial Discovery Agent (0.3–0.5).
- Log the full conversation between Analyst → Skeptic → Moderator for auditability.
- In the Refinement Loop, inject the current episodic memory into the Initial Discovery Agent prompt so it can discover previously missed entities with better context.

---

**End of Implementation Plan**
