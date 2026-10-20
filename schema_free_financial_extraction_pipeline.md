# Sequential Hybrid Pipeline for Schema-Free Entity Extraction from Financial Documents

**Context**  
- Process documents **sequentially** (one or a small stream at a time).  
- Start from **Google Document AI** OCR / Layout Parser output (structured text + tables + hierarchy).  
- Only closed models available: **Gemini** and **Grok**.  
- Auto-infer schema / taxonomy from the documents themselves.  
- Use classical ML nodes (especially clustering) + closed-model induction.  
- Extract primitives and compute derivatives.  
- Optional lightweight taxonomy / ontology grounding (e.g., FIBO).

State (taxonomy, embeddings, cluster model, extracted instances) persists and improves across documents.

---

## 1. Document AI Processing (Layout Parser preferred)

**What it does**  
Convert the PDF into structured text with layout preserved.

**Expected output**  
Document proto (or JSON) containing:
- Full text
- Hierarchical layout (headings → sections → paragraphs)
- Detected tables (with cells)
- Form fields / key-value pairs
- Bounding boxes / page anchors

**Implementation**  
Call Google Document AI Gemini Layout Parser (or Enterprise Document OCR + Form Parser).  
Extract the `text`, `pages[].paragraphs`, `tables`, and any `formFields`.  
Keep the original structure rather than flattening to a single string so later steps can process sections and tables independently.

---

## 2. Structure-Aware Chunking

**What it does**  
Split the Doc AI output into pieces that fit the embedding and LLM context windows while respecting document structure.

**Expected output**  
List of chunks, each with:
- `chunk_id`
- `text`
- `type` (section / table / paragraph)
- `page`
- `section_path` (e.g. “Covenants > Financial Covenants”)
- Optional table JSON

**Implementation**  
Walk the Doc AI layout tree.  
Emit one chunk per major section or per table.  
For long sections, further split at paragraph boundaries so each chunk stays under ~4–6k tokens.  
Preserve metadata so provenance is never lost.

---

## 3. Embedding of Chunks and Candidate Attributes

**What it does**  
Create vector representations for similarity, clustering, and incremental assignment.

**Expected output**  
Embeddings (float vectors) for every chunk and for any candidate attribute strings. Stored with their IDs.

**Implementation**  
Call Gemini Embedding (task type `CLUSTERING` or `SEMANTIC_SIMILARITY`) on each chunk text.  
Optionally extract lightweight candidate spans (monetary amounts, dates, rate phrases) with simple patterns or a cheap Gemini call, then embed those too.  
Store in a vector store or simple matrix for the clustering node.

---

## 4. Clustering and Incremental Assignment (HDBSCAN)

**What it does**  
Discover natural groups of sections, document subtypes, or attributes without knowing the number of groups in advance. Assign new documents to existing groups.

**Expected output**  
- Cluster labels for chunks/attributes  
- Noise flags  
- Cluster centroids (or a fitted HDBSCAN model)  
- New documents receive predicted cluster IDs

**Implementation**  
Reduce dimensionality with UMAP if needed, then fit HDBSCAN on the embeddings (tune `min_cluster_size` / `min_samples`).  
For subsequent documents, use approximate nearest-neighbor assignment or HDBSCAN’s prediction utilities to place new chunks into existing clusters or flag them as novel.  
Persist the model or centroids.

### Clarification: Is this where the schema is auto-inferred?
**No.**  
Step 4 only discovers **natural groups**. It answers questions such as:
- Which sections look similar?
- Which attribute phrases cluster together?
- Which documents share a subtype?
- What is noise?

You receive groups (e.g., “cluster 3 = similar rate-related phrases”), **not** named concepts, a hierarchy, field definitions, or a JSON schema.

Schema / taxonomy auto-inference happens in **Step 5**.

---

## 5. Incremental Schema / Taxonomy Induction

**What it does**  
Turn the groups from Step 4 (plus open extractions) into a usable, evolving schema/taxonomy. It is incremental: each new document can add, merge, or refine concepts.

**Inputs**
- Clusters from Step 4 (groups of similar chunks or candidate attributes + embeddings + example texts)
- Open extractions from the current document (candidate triples / attributes proposed by Gemini/Grok)
- Current persistent taxonomy from previous documents
- Optional: FIBO (or another ontology) for alignment

**How it works (sub-steps)**

1. **Collect candidates**  
   Prompt Gemini/Grok (structured output) on relevant chunks:  
   “Extract candidate entity types, attributes, and relations. Return {name, description, example_values, source_chunk}.”  
   Also pull members of promising new or low-frequency clusters.

2. **Governance / filtering**  
   Discard candidates with no clear evidence in the text, pure noise, or conflicts with already-stable concepts.  
   Use simple rules + a short LLM verification call.

3. **Cluster-aware merging and naming**  
   Embed new candidate names/descriptions.  
   Assign to existing clusters or form new ones.  
   For stable clusters, ask the model:  
   “These surface forms appear together: [list]. Propose one canonical concept name, short definition, parent concept if obvious, and typical value type.”

4. **Hierarchy and normalization**  
   Place new concepts into the existing taxonomy (parent/child).  
   Optional FIBO alignment: embed concept name + definition, compare to FIBO classes, confirm with LLM.

5. **Update statistics and commit**  
   Increment frequencies, record surface forms, mark concepts “stable” after consistent appearance.  
   Write updated taxonomy to persistent state.

**Expected output**  
Updated taxonomy / schema (JSON or lightweight graph) containing:
- Canonical concept ID and name
- Description
- Parent concept (hierarchy)
- Value type / constraints
- Example surface forms
- Frequency and stability flag
- Optional FIBO mapping

**Example fragment**
```json
{
  "concept_id": "commitment_amount",
  "name": "Commitment Amount",
  "description": "Maximum principal amount the lender agrees to make available",
  "parent": "monetary_obligation",
  "value_type": "money",
  "surface_forms": ["Commitment", "Total Commitment", "Aggregate Commitments"],
  "frequency": 17,
  "stable": true,
  "fibo_mapping": "fibo-fbc-fi-fi:Commitment"
}
```

**Implementation**  
Load current taxonomy + new clusters/candidates.  
Prompt Gemini/Grok for open candidates and for canonical naming of clusters.  
Filter with a short governance check.  
Merge via embedding similarity + LLM hierarchy proposal (and optional FIBO lookup).  
Persist the updated taxonomy.

---

## 6. Primitive Extraction Against Current Taxonomy

**What it does**  
Fill the (now partially known) schema with concrete values from the current document, with provenance and confidence.

**Expected output**  
Structured JSON (or graph fragment) of primitives:
- Parties, monetary amounts with roles, dates, rates, covenants/thresholds, events, etc.
- Each with `value`, `confidence`, `source_chunk` / `page`, and `concept_id` from the taxonomy.

**Implementation**  
Prompt Gemini or Grok with the current taxonomy schema + relevant chunks (or full structured Doc AI output for short docs).  
Enforce structured JSON output.  
Add a light validation pass (numeric consistency, unit checks, or a second reflection call).  
Low-confidence or novel spans are sent back to the induction step.

---

## 7. Deterministic Derivative Computation

**What it does**  
Compute secondary values that can be derived reliably from the primitives.

**Expected output**  
Additional fields or a small derived graph:
- Ratios (leverage, interest coverage)
- Covenant compliance flags
- Net exposures
- Simple risk signals
- Resolved entity links

**Implementation**  
Once primitives are in structured form, run ordinary Python/pandas or a rule engine.  
Examples: compare a reported ratio against a covenant threshold, compute outstanding / commitment, flag missing required fields.  
Use the closed model only when the derivation requires interpreting ambiguous language.

---

## 8. Persist State for Next Document

**What it does**  
Save everything needed so the next document benefits from prior knowledge.

**Expected output**  
Updated persistent stores:
- Taxonomy
- Embedding index
- HDBSCAN model / centroids
- Extracted instances
- Simple statistics (concept frequencies, consistency scores)

**Implementation**  
Write the taxonomy JSON, append new embeddings, update cluster model, and store the document’s extracted primitives + derivatives.  
On the next document the induction and extraction steps load this state first.

---

## Summary of Responsibilities

| Step | Role | Produces Schema? |
|------|------|------------------|
| 1–3  | Prepare structured text, chunks, embeddings | No |
| 4    | Discover natural groups (HDBSCAN) | No |
| 5    | Auto-infer / evolve taxonomy & schema | **Yes** |
| 6    | Extract primitives against schema | Uses schema |
| 7    | Compute derivatives | Uses primitives |
| 8    | Persist state | — |

After each document you obtain structured primitives grounded in the growing taxonomy, computed derivatives, and an improved schema for the documents that follow.
