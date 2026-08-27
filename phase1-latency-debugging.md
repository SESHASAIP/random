# Phase 1 Latency Debugging — 45 min → <2 min

**Context:** ~125 segments per doc, Gemini Flash short analysis, concurrency 8, 45 min wall clock. 9 phases total, 15–20 docs.

**The math that drives this doc:** 45 min ÷ (125 ÷ 8) ≈ **170s per segment slot**. A short Flash call should be 3–10s. You are ~20–30x off. That gap is a leak, not an architecture limit.

Work these in order. Each step either explains the gap or rules itself out.

---

## Step 0 — Freeze the baseline

Before touching anything:

- Run phase 1 on 2 docs, save full output to disk. This is your **golden set**.
- Record total wall clock as measured today, same machine, same quota, same region.

Every later change gets diffed against the golden set. Without this you cannot claim "same level of analysis" — you can only hope.

---

## Step 1 — Instrument every call

Log one row per segment to JSONL:

```
segment_id, t_submit, t_first_token, t_complete,
input_tokens, output_tokens, thinking_tokens,
http_status, attempt_number, finish_reason, error
```

Field sources:

| Field | Where it comes from |
|---|---|
| `thinking_tokens` | `usage_metadata.thoughts_token_count` |
| `input_tokens` | `usage_metadata.prompt_token_count` |
| `output_tokens` | `usage_metadata.candidates_token_count` |
| `finish_reason` | response candidate — `MAX_TOKENS` means truncation, possibly silent retries |

Run one doc. Every step below reads off this file.

---

## Step 2 — Is your concurrency real?

Highest-probability culprit. Check it first.

```python
import pandas as pd

df = pd.read_json("calls.jsonl", lines=True)
edges = pd.concat([
    pd.DataFrame({"t": df.t_submit,   "d":  1}),
    pd.DataFrame({"t": df.t_complete, "d": -1}),
]).sort_values("t")
edges["inflight"] = edges.d.cumsum()
print(edges.inflight.describe())
```

**Read it:** median in-flight should be ~8. If it reads 1–2, your parallelism is fake.

Usual causes:

- Sync `google-genai` client called inside an async context
- Blocking call inside `asyncio.gather` (no `await`, no thread offload)
- ADK / LangGraph node serializing internally despite the fan-out

**Stop here and fix it.** Nothing below matters until this reads ~8.

---

## Step 3 — Split wall clock into its parts

From the same file:

```python
theoretical = (df.t_complete - df.t_submit).sum() / 8   # at true concurrency 8
median_call = (df.t_complete - df.t_submit).median()
idle_gap    = actual_wall_clock - theoretical
```

**Read it:**

- `median_call ≈ 10s` but wall clock is 45 min → time is in **gaps** (scheduling, barriers, backoff sleeps)
- `median_call ≈ 150s` → time is in the **model** → go to Step 4

---

## Step 4 — Check thinking tokens

Histogram `thinking_tokens`. On 2.5 Flash, dynamic thinking is on by default and will burn 2–4k reasoning tokens on a task that needs none.

Test directly:

```python
from google.genai import types
config = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=0),
)
```

Rerun 20 segments. Compare **both**:

1. Latency delta
2. Output vs golden set

If quality holds, this is often several multiples of speedup in one config line.

---

## Step 5 — Count retries and 429s

Group by `http_status` and `attempt_number`. Compute seconds spent in backoff sleep **separately** from seconds waiting on the model.

**Read it:** 429s at concurrency 8 means you have a quota problem, not a throughput problem. Check Vertex RPM/TPM for your project and region before raising concurrency — going to 64 will make it dramatically worse until quota is raised.

Also confirm: is retry time included in your 45 min figure? If retries are swallowed silently, your "model latency" is fiction.

---

## Step 6 — Measure prompt redundancy

```python
fixed_overhead = df.input_tokens.min()      # roughly the re-sent prefix
wasted = fixed_overhead * len(df)
```

`fixed_overhead` is your system instruction + schema + few-shots + doc-level context, re-prefilled on all ~125 calls.

**Read it:** if that's 5k+ tokens per call, explicit context caching is a real win — cache once per document, send only the segment per call.

---

## Step 7 — Check output size distribution

Histogram `output_tokens`, compare p50 vs p99.

**Read it:** if p99 is 5–10x p50, a handful of runaway generations are holding concurrency slots for minutes and dragging wall clock. Inspect those segments by hand — usually one that triggered prose rambling instead of structured output.

Fix: strict `response_schema` + a real `max_output_tokens` ceiling so no single call can hold a slot for 170s.

---

## Step 8 — Find the duplicate segments

```python
import hashlib

norm = lambda s: " ".join(s.lower().split())
df["h"] = df.segment_text.map(lambda s: hashlib.sha256(norm(s).encode()).hexdigest())

print(df.h.duplicated().mean())                      # within one doc
# repeat with all 15-20 docs pooled
```

**Read it:** that percentage is calls you can delete outright with zero quality risk. Headers, definitions, standard clauses, references. In same-domain corpora this routinely kills 20–40%.

---

## What you should have at the end

One sentence with real numbers:

> "45 min = 6 min of actual model time + 31 min of idle gap from fake concurrency + 8 min of backoff sleep."

Once you can write that, the fix picks itself.

---

## Fix levers, ranked (apply after diagnosis)

| Lever | Typical win | Quality risk |
|---|---|---|
| Fix fake concurrency | Large | None |
| `thinking_budget=0` | Large | Verify vs golden set |
| Wide bounded fan-out (48–96, quota-sized) | Large | None (watch 429s) |
| Explicit context caching | Moderate | None |
| Strict `response_schema` + token ceiling | Moderate | Low |
| Content-hash dedupe | 20–40% of calls | None |
| Micro-batch 4–6 segments/call | Large | **Highest — do last, A/B it** |

Micro-batching is where analysis depth actually degrades (model gets lazier on segments 4–6). Given the no-compromise constraint, treat it as the last lever, not the first.

---

## Beyond phase 1

If phase 2 needs only a *segment's* phase-1 output — not the whole document's — don't gate on phase 1 completing.

Turn the pipeline into a **per-segment streaming DAG**: each segment flows into phase 2 as soon as it finishes phase 1. Nine sequential wall clocks collapse to roughly `max(phase_latency) + pipeline_depth`, and fan-out saturates quota continuously instead of in bursts with idle gaps between phases.

Phases that genuinely need a document-level reduce (cross-segment consistency, global summary) become explicit **barrier nodes**. Everything else streams.

In LangGraph: a per-segment subgraph with `Send`-based fan-out, rather than phase-level nodes.

Fixing phase 1 alone gets you to 2 min. The streaming DAG is what makes 20 docs × 9 phases tractable.
