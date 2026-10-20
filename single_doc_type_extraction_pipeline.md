# Simplified Sequential Pipeline for Single Document Type
## Schema-Free Entity Extraction from Financial Documents

**Context**  
All documents belong to roughly the **same family** (e.g. all credit agreements, all 10-K filings, all bank statements, all loan applications).  
- Process documents **sequentially**.  
- Start from **Google Document AI** (Layout Parser preferred) structured output.  
- Only closed models: **Gemini** and **Grok**.  
- Auto-infer schema / taxonomy from the documents.  
- Classical ML (clustering) used mainly for attribute and section discovery inside the single type.  
- Optional lightweight ontology grounding (e.g. FIBO).

Because the document type is uniform, schema induction stabilizes much faster and the overall pipeline is lighter than the multi-type version.

---

## High-Level Differences vs Heterogeneous Documents

| Aspect                        | Heterogeneous docs          | Single doc type                     |
|-------------------------------|-----------------------------|-------------------------------------|
| Document-level clustering     | Important                   | Largely unnecessary                 |
| Schema induction              | Ongoing, heavier            | Stabilizes after ~5–15 documents    |
| Main use of clustering        | Discover subtypes           | Normalize attributes & sections     |
| Incremental updates           | Frequent schema evolution   | Mostly refinement & edge cases      |
| Practical freeze point        | Later                       | After initial induction phase       |

---

## 1. Document AI Processing (Layout Parser preferred)

**What it does**  
Convert each PDF into structured text with layout preserved.

**Expected output**  
Document proto / JSON containing:
- Full text
- Hierarchical layout (headings → sections → paragraphs)
- Detected tables (cells, structure)
- Form fields / key-value pairs
- Bounding boxes and page anchors

**Implementation**  
Call Google Document AI Gemini Layout Parser (or Enterprise Document OCR + Form Parser).  
Extract text, paragraphs, tables, and form fields.  
Retain structure so later steps can process sections and tables independently.

---

## 2. Structure-Aware Chunking

**What it does**  
Split the Doc AI output into context-window-friendly pieces while respecting document structure.

**Expected output**  
List of chunks with:
- `chunk_id`, `text`, `type` (section / table / paragraph)
- `page`, `section_path`
- Optional structured table JSON

**Implementation**  
Walk the layout tree. Emit one chunk per major section or table.  
Further split long sections at paragraph boundaries so each chunk stays under ~4–6k tokens.  
Preserve metadata for provenance.

---

## 3. Embedding of Chunks and Candidate Attributes

**What it does**  
Create vectors for attribute discovery, section similarity, and outlier detection inside the single document type.

**Expected output**  
Embeddings for every chunk and for candidate attribute strings, stored with IDs.

**Implementation**  
Call Gemini Embedding (`CLUSTERING` or `SEMANTIC_SIMILARITY` task type) on chunk text.  
Optionally extract lightweight candidate spans (monetary amounts, dates, rates, party names) via patterns or a cheap LLM pass, then embed them.  
Store embeddings for the clustering and induction steps.

---

## 4. Clustering (HDBSCAN) — Attribute & Section Focus

**What it does**  
Discover natural groups of **attributes** and **section types** within the single document family. No longer used for discovering document subtypes.

**Expected output**  
- Cluster labels for attribute phrases and section chunks  
- Noise / outlier flags  
- Cluster centroids or fitted model  
- Incremental assignment of new chunks to existing clusters

**Implementation**  
Optionally reduce dimensions with UMAP.  
Fit HDBSCAN on attribute embeddings and/or section embeddings (tune `min_cluster_size`).  
For new documents, assign chunks via nearest centroid or HDBSCAN prediction utilities.  
Persist centroids/model. Primary value is grouping synonymous surface forms (e.g. “Commitment Amount”, “Total Facility”, “Aggregate Commitments”) and similar sections.

### Note
This step does **not** produce the schema. It only creates groups that Step 5 turns into named concepts.

---

## 5. Incremental Schema / Taxonomy Induction (Simplified)

**What it does**  
Build and refine a taxonomy from open extractions + clusters. With a single document type this stabilizes quickly.

**Inputs**
- Clusters from Step 4
- Open candidate extractions from the current document
- Current persistent taxonomy
- Optional FIBO (or other ontology) definitions

**How it works**

1. **Initial phase (first 8–12 documents)**  
   Prompt Gemini/Grok for open candidates:  
   “Extract important entity types, attributes, and relations. Return structured list with name, description, example values, source.”  
   Cluster the candidates. Ask the model to propose canonical names, definitions, value types, and hierarchy.

2. **Governance**  
   Keep only candidates with clear textual evidence. Filter noise and conflicts with already-stable concepts.

3. **Naming, merging, hierarchy**  
   For each stable cluster, obtain one canonical concept.  
   Place concepts into a hierarchy.  
   Optionally align high-frequency concepts to FIBO via embedding similarity + LLM confirmation.

4. **Stabilization**  
   After the initial documents, mark high-frequency concepts as stable.  
   Later documents mainly add surface forms or rare edge-case fields.  
   Re-open full induction only when a high-confidence novel concept appears.

**Expected output**  
Evolving taxonomy (JSON or lightweight graph):
- Canonical concept ID & name
- Description
- Parent concept
- Value type
- Surface forms seen
- Frequency & stability flag
- Optional FIBO mapping

**Example**
```json
{
  "concept_id": "commitment_amount",
  "name": "Commitment Amount",
  "description": "Maximum principal the lender agrees to advance",
  "parent": "monetary_obligation",
  "value_type": "money",
  "surface_forms": ["Commitment", "Total Commitment", "Aggregate Commitments", "Facility Amount"],
  "frequency": 23,
  "stable": true,
  "fibo_mapping": "fibo-fbc-fi-fi:Commitment"
}
```

**Implementation**  
Load current taxonomy.  
Run open extraction + cluster naming prompts on Gemini/Grok.  
Apply governance filter.  
Merge into hierarchy (and optional FIBO alignment).  
Persist updated taxonomy. After the initial phase, switch most documents to extraction-only mode.

---

## 6. Primitive Extraction Against Current Taxonomy

**What it does**  
Extract concrete values for the known concepts from the current document, with provenance and confidence.

**Expected output**  
Structured primitives (JSON or graph fragment):
- Parties, amounts (with roles), dates, rates, covenants/thresholds, events, etc.
- Each value linked to `concept_id`, confidence, and source chunk/page.

**Implementation**  
Prompt Gemini or Grok with the current taxonomy + relevant chunks (or full structured output for shorter documents).  
Enforce structured JSON.  
Run a light validation / reflection pass (numeric consistency, unit checks).  
Send low-confidence or clearly novel spans back to induction if needed.

---

## 7. Deterministic Derivative Computation

**What it does**  
Compute secondary values from the primitives using code.

**Expected output**  
Derived fields such as:
- Financial ratios
- Covenant compliance flags
- Net exposures
- Simple risk signals
- Cross-field consistency checks

**Implementation**  
Apply pandas / rule engine / simple formulas on the structured primitives.  
Use the closed model only for ambiguous interpretive cases.  
Most derivatives stay fully deterministic.

---

## 8. Persist State

**What it does**  
Save taxonomy, embeddings, cluster model, and extracted results so the next document benefits.

**Expected output**  
Updated persistent stores (taxonomy JSON, embedding index, HDBSCAN centroids, extracted instances, statistics).

**Implementation**  
Write taxonomy and append new embeddings/instances.  
On the next document, load this state before induction or extraction.

---

## Recommended Operating Mode for Single Document Type

1. **Bootstrap (documents 1–12)**  
   Run full pipeline including open induction (Steps 1–5).  
   Review the resulting taxonomy once (spot-check high-frequency concepts).

2. **Stabilize**  
   Freeze or lightly lock the core schema.  
   Mark stable concepts.

3. **Steady state (remaining documents)**  
   - Chunk + embed  
   - Extract against the frozen taxonomy (Step 6)  
   - Compute derivatives (Step 7)  
   - Lightweight novel-concept detector: only trigger re-induction when something clearly new and high-confidence appears  
   - Continue background attribute/section clustering for monitoring wording drift

4. **Optional periodic refresh**  
   Every N documents, re-cluster recent attributes and ask the LLM whether any merges or new parent concepts are warranted.

---

## Summary

| Step | Role in single-doc-type setting                  | Produces schema? |
|------|--------------------------------------------------|------------------|
| 1–3  | Prepare structured text, chunks, embeddings      | No               |
| 4    | Group attributes & sections (HDBSCAN)            | No               |
| 5    | Auto-infer and quickly stabilize taxonomy        | **Yes**          |
| 6    | Extract primitives against taxonomy              | Uses schema      |
| 7    | Compute derivatives                              | Uses primitives  |
| 8    | Persist state                                    | —                |

With a single document type the induction phase is front-loaded. After a modest number of documents the schema is stable, extraction becomes consistent, and most subsequent work is straightforward structured extraction plus deterministic derivation. Clustering remains valuable for normalizing surface forms and detecting internal variation, but is no longer needed for discovering document subtypes.
