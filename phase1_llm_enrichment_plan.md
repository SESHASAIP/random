# Phase 1 Implementation Plan
## LLM Enrichment Layer on Top of Deterministic Section Splitter

**Version:** 1.0  
**Date:** 2026-08-25  
**Scope:** Only the intelligent analysis addition for Phase 1

---

## 1. Goal

Take the output of the existing deterministic section splitter and enrich every section with:

- A high-quality short factual analysis
- Preliminary signals
- Simple boolean flags
- Potential entity roles

This enriched data becomes the foundation for the document graph (Phase 2) and later SubEntity creation.

**Key constraint:**  
Do not change or re-implement the deterministic splitter. Only add an enrichment layer on top of it.

---

## 2. High-Level Flow

```text
Document .txt
      │
      ▼
Existing Deterministic Splitter
      │
      ▼
List[RawSection]          ← (title, raw_text, level, start_char, end_char, section_id)
      │
      ▼
Phase 1 Enrichment Layer (this plan)
      │  for each RawSection:
      │      → build prompt
      │      → call LLM (structured output)
      │      → validate & parse
      │      → merge into EnrichedSection
      ▼
List[EnrichedSection]
      │
      ▼
Phase 2 (Graph Construction)
```

---

## 3. Data Models

### 3.1 Input (from existing deterministic code)

```python
@dataclass
class RawSection:
    section_id: str
    title: str
    raw_text: str
    level: int
    start_char: int
    end_char: int
    parent_id: Optional[str] = None
```

### 3.2 Output (enriched)

```python
@dataclass
class EnrichedSection:
    # --- original fields (passed through) ---
    section_id: str
    title: str
    raw_text: str
    level: int
    start_char: int
    end_char: int
    parent_id: Optional[str] = None

    # --- new fields added by LLM ---
    short_analysis: str
    preliminary_signals: List[str]
    contains_parties: bool
    contains_risk_language: bool
    potential_entities: List[str]
    
    # optional metadata
    llm_model: str = ""
    llm_confidence: Optional[float] = None
```

---

## 4. LLM Call Design

### 4.1 System Prompt

```text
You are a precise legal document analyst specializing in wholesale credit agreements.

Your only job is to analyze one section and return structured factual information.
Do not invent details. Do not speculate. Do not rephrase the section unnecessarily.
```

### 4.2 User Prompt Template

```text
Analyze the following section of a credit agreement.

Section ID: {section_id}
Section Title: {title}

Section Text:
\"\"\"
{raw_text}
\"\"\"

Return a JSON object with exactly these keys:

{
  "short_analysis": "3 to 6 factual sentences describing the purpose of this section and any notable parties, risk language, or extractable values",
  "preliminary_signals": ["list", "of", "interesting", "surface", "phrases"],
  "contains_parties": true/false,
  "contains_risk_language": true/false,
  "potential_entities": ["administrator", "risk_level", ...]
}

Rules:
- short_analysis must be factual and concise
- preliminary_signals should only contain phrases that actually appear in the text
- potential_entities should use simple role names (administrator, borrower, risk_level, lender, agent, etc.)
- If nothing relevant is found, use empty lists and false flags
- Output valid JSON only. No markdown, no extra text.
```

### 4.3 Expected LLM Output Example

```json
{
  "short_analysis": "This section formally appoints the fund administrator and defines its primary duties and reporting obligations. It identifies the named administrative entity responsible for the facility. No risk rating or classification language is present in this section.",
  "preliminary_signals": ["fund administrator", "Administrator shall be"],
  "contains_parties": true,
  "contains_risk_language": false,
  "potential_entities": ["administrator"]
}
```

---

## 5. Implementation Structure

### 5.1 Main Function Signature

```python
def enrich_sections_with_llm(
    raw_sections: List[RawSection],
    llm_client: Any,                    # your LLM wrapper
    model_name: str = "your-strong-model",
    max_concurrency: int = 5,
    cache: Optional[Dict] = None
) -> List[EnrichedSection]:
    """
    Takes the output of the deterministic splitter and returns enriched sections.
    """
```

### 5.2 Recommended Internal Steps

1. **Prepare prompts** for all sections
2. **Execute LLM calls** (with concurrency control)
3. **Validate** each response (required keys, types, non-empty analysis)
4. **Retry** once on invalid JSON or missing keys
5. **Fallback** on permanent failure:
   - `short_analysis` = first 2–3 sentences of raw_text or a safe default
   - empty lists + false flags
6. **Merge** original RawSection fields + LLM fields into `EnrichedSection`
7. **Return** ordered list (same order as input)

### 5.3 Validation Rules

- `short_analysis` must be a non-empty string (min 40 characters recommended)
- `preliminary_signals` must be a list of strings
- `contains_parties` and `contains_risk_language` must be boolean
- `potential_entities` must be a list of strings
- Reject responses that contain markdown code fences or extra explanatory text

---

## 6. Error Handling & Resilience

| Situation                        | Action                                      |
|----------------------------------|---------------------------------------------|
| Invalid JSON                     | Retry once with temperature=0               |
| Missing required keys            | Retry once                                  |
| Second failure                   | Use deterministic fallback                  |
| Empty section text               | Skip LLM, set safe defaults                 |
| Very long section (> 8k tokens)  | Truncate intelligently or split further     |
| Rate limit / timeout             | Exponential backoff + retry                 |

Always log:
- section_id
- success / failure
- model used
- latency
- whether fallback was used

---

## 7. Performance Considerations

- Use concurrent calls (asyncio or thread pool) with a safe concurrency limit (3–8)
- Cache results by `document_id + section_id + content_hash` so re-runs are cheap
- Prefer a model that is good at following JSON instructions and staying factual
- Keep temperature low (0.0 – 0.2)

---

## 8. Integration Contract

**Upstream:**  
Existing deterministic splitter → `List[RawSection]`

**Downstream:**  
`List[EnrichedSection]` is passed directly to Phase 2 (Graph Construction).  
Phase 2 should store `short_analysis` on every graph node.

**No other phase should call the LLM for basic section understanding.**

---

## 9. Testing Plan

1. **Unit test** with 3–5 synthetic sections (party section, risk section, boilerplate section)
2. **Validation test** – force malformed LLM responses and verify fallback works
3. **End-to-end test** – run deterministic splitter → enrichment → check that every section has a usable `short_analysis`
4. **Regression test** – ensure original fields (`start_char`, `end_char`, `section_id`) are never altered

---

## 10. Implementation Checklist

- [ ] Define `RawSection` and `EnrichedSection` dataclasses
- [ ] Write the exact system + user prompt templates
- [ ] Implement `enrich_sections_with_llm()`
- [ ] Add JSON validation + retry logic
- [ ] Add deterministic fallback
- [ ] Add concurrency control
- [ ] Add caching by content hash
- [ ] Add structured logging
- [ ] Write unit tests with mocked LLM responses
- [ ] Wire into the main pipeline (after deterministic splitter, before Phase 2)

---

## 11. Design Principles Applied

- Deterministic code remains the source of truth for structure
- LLM is used only for the narrow enrichment task
- Single-shot structured generation (no multi-step agent reasoning pattern)
- Full observability and safe fallbacks
- Clean separation so the enrichment layer can be improved independently

---

*This plan is the definitive specification for the Phase 1 LLM enrichment layer.*
"""
