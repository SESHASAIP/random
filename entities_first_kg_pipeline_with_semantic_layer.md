# Schema-Free Entity Extraction & Knowledge Graph Pipeline
## Entities-First Approach for Financial Documents (Single Document Type)

**Version**: Consolidated with multi-document incremental flow, lightweight semantic layer, and future semantic layer usage for prompts, extraction, and business rules.

**Context**
- Sequential processing of multiple documents of the **same type** (e.g. credit agreements).
- No predefined schema — auto-infer taxonomy from the documents.
- Closed models only (Gemini / Grok).
- Google Document AI OCR/Layout Parser as starting point.
- Lightweight knowledge graph (taxonomy + instances).
- No full temporal KG required for the core problem.
- Graph database (Neo4j) optional — not mandatory.

---

## High-Level Pipeline (Entities-First)

1. **Document AI Processing** → structured text + layout
2. **Structure-Aware Chunking / Distillation** → semantic blocks
3. **Open Entity & Attribute Extraction** (no schema required)
4. **Clustering & Entity Resolution** (HDBSCAN + embeddings)
5. **Incremental Taxonomy Induction** → growing concept layer
6. **Relation Extraction & Graph Assembly**
7. **Deterministic Derivative Computation** (code)
8. **Persist State** (taxonomy + instances + optional rules)

With multiple documents the process is **incremental**: each new document is extracted and then merged into the existing graph/taxonomy.

---

## Step-by-Step with Expected Outputs

### 1. Document AI Processing
**Output**: Structured text, sections, tables, form fields with provenance.

### 2. Chunking / Document Distillation
**Output**: List of clean semantic blocks (atomic facts), each with `block_id`, text, and metadata.

### 3. Open Entity & Attribute Extraction
**Input**: Semantic blocks  
**Output**: Raw entities and attributes (Borrower, Commitment Amount, Applicable Margin, Covenants, Dates, etc.) with source references.  
No taxonomy is required at this stage.

### 4. Clustering & Entity Resolution
**Output**: Grouped surface forms (e.g. “Total Commitment”, “Aggregate Commitments”, “Facility Amount” → same concept). Noise/outlier flags.

### 5. Incremental Taxonomy Induction (Lightweight Semantic Layer)
**Output**: Evolving taxonomy of canonical concepts:
- Concept ID & name
- Description
- Parent concept
- Value type
- Surface forms seen
- Frequency & stability flag
- Optional FIBO mapping

This taxonomy is the **lightweight semantic layer**.

### 6. Relation Extraction & Graph Assembly
**Output**: Nodes (entities/instances) + edges (relations) with provenance.  
New documents add or link to existing nodes.

### 7. Deterministic Derivative Computation
**Output**: Calculated fields (ratios, compliance flags, exposures, etc.) produced by code.

### 8. Persist State
**Output**: Updated taxonomy, embeddings, cluster model, extracted instances, and (later) rules.

---

## Multi-Document Behavior

| Stage | What Happens |
|-------|--------------|
| Document 1 | Full open extraction → initial taxonomy + graph |
| Documents 2–15 | Extract → resolve against existing taxonomy/graph → add new instances or strengthen concepts |
| After stabilization | Mostly instance addition; taxonomy changes become rare |
| Novel concept detected | Trigger light re-induction for that concept only |

---

## Storage Options

| Option | Graph DB required? | Recommended for |
|--------|--------------------|-----------------|
| JSON / files | No | POC |
| Postgres (relational) | No | Production start |
| Neo4j (Community Edition – open source) | Yes | Relationship-heavy queries, visualization, GraphRAG |
| Hybrid | Optional | Taxonomy in Postgres + graph views in Neo4j |

Neo4j Community Edition is free and open source (GPL).

---

## Lightweight Semantic Layer Today

What you get automatically:
- Canonical concept taxonomy
- Surface-form normalization
- Instance linking
- Provenance

What you do **not** get automatically:
- Executable business rules
- Prompt templates
- Formal constraints / reasoner

---

## Future Use of the Semantic Layer
### (How to evolve it for prompts, extraction, and rules)

Once the taxonomy stabilizes, the semantic layer becomes the single source of truth for three important capabilities:

### 1. Base Prompt Creation for Entities
Store prompt fragments or full extraction prompts next to each concept.

**Example structure**:
```json
{
  "concept_id": "commitment_amount",
  "name": "Commitment Amount",
  "extraction_prompt": "Extract the total commitment / facility amount. Return the numeric value, currency, and the exact surface form used in the text. Source: {chunk}",
  "examples": ["Total Commitment is $150,000,000", "Aggregate Commitments equal USD 75 million"]
}
```

**Usage**: When processing a new document, dynamically assemble the extraction prompt from the stable concepts instead of writing a giant free-form prompt.

### 2. Guided Value Extraction for Entities
After the taxonomy exists, switch from fully open extraction to **constrained extraction**:

- Load the current taxonomy.
- For each stable concept, run a focused extraction prompt (or a single structured prompt that lists all known concepts).
- Validate the returned values against the concept’s `value_type`.
- Only fall back to open extraction for spans that do not match any known concept (novelty detection).

This improves consistency and reduces noise.

### 3. Complex Derivative Business Rules
Attach executable rules to concepts (or groups of concepts).

**Example rule objects**:
```json
{
  "rule_id": "leverage_covenant_check",
  "applies_to": ["consolidated_leverage_ratio"],
  "type": "threshold",
  "expression": "extracted_value <= threshold",
  "threshold_source": "document",
  "output": "covenant_compliance_flag"
}
```

```json
{
  "rule_id": "interest_rate_calculation",
  "applies_to": ["applicable_margin", "benchmark_rate"],
  "type": "formula",
  "expression": "benchmark_rate + applicable_margin",
  "output": "all_in_interest_rate"
}
```

**Execution**:
- After primitive extraction, a deterministic engine (Python, rules engine, or simple evaluator) runs the attached rules.
- Results are stored as derived nodes/edges or additional fields.
- The closed model is used only for discovering or explaining ambiguous rules, not for executing hard constraints.

### Recommended Evolution Path
1. Bootstrap with open entities-first extraction (current pipeline).
2. Stabilize taxonomy after 8–15 documents.
3. Add prompt templates to concepts → switch to guided extraction.
4. Add structured rule objects for key derivatives and covenants.
5. (Optional) Ground high-value concepts in FIBO.
6. (Optional) Move to Neo4j if rich relationship queries become important.

---

## Summary

| Capability | Built automatically? | How to add later |
|------------|----------------------|------------------|
| Concept taxonomy | Yes | — |
| Instance graph | Yes | — |
| Extraction prompts | No | Attach prompt templates to concepts |
| Guided extraction | No | Use taxonomy to constrain LLM calls |
| Business / derivative rules | No | Store rule objects + execute in code |
| Full temporal KG | No | Add only if historical “as-of” queries are required |
| Graph database | No | Neo4j optional |

This pipeline gives you an auto-inferred, incrementally growing semantic structure from financial documents. The semantic layer starts as a taxonomy and can later be extended into the foundation for consistent prompts, reliable value extraction, and deterministic business rules.
