# ADK Dry-Run Agent Trajectory Visualization

**End-to-End Implementation Plan**

---

## 1. Goal

Build a lightweight, near real-time dry-run experience inside a visual builder for Google ADK agents.

When a user triggers a **Dry Run**:

1. The agent (or multi-agent graph) executes.
2. Every sub-agent, LLM call, and tool call is captured as OpenTelemetry spans.
3. The spans are converted into a clean **nodes + edges** graph.
4. The graph is returned to the UI in the same response.
5. The UI renders a flow diagram showing input → output for every step.
6. Nothing is persisted. The data lives only for the duration of that request.

---

## 2. Terminology

| Term | Meaning |
|------|---------|
| **Dry Run** | One-time execution of an agent (or multi-agent graph) triggered from the visual builder for inspection only |
| **Span** | Single unit of work captured by OpenTelemetry (agent run, LLM call, tool call, etc.) |
| **Trace** | Complete tree of spans that share the same `trace_id` for one dry run |
| **InMemorySpanExporter** | OpenTelemetry component that keeps finished spans only in memory |
| **Trajectory Graph** | The final structure returned to the UI: `{ nodes: [...], edges: [...] }` |
| **Node** | One step in the graph (Agent, LLM, Tool, Chain, etc.) |
| **Edge** | Parent → child relationship between two nodes |
| **gRPC Agent Runtime** | The service that actually executes the ADK agents |
| **API Layer** | HTTP service that the UI calls; it talks to the gRPC runtime |
| **Visual Builder UI** | Frontend that shows the flow diagram |

---

## 3. High-Level Architecture

```
UI (Visual Builder)
    │  HTTP POST /dry-run
    ▼
API Layer
    │  gRPC DryRunRequest
    ▼
gRPC Agent Runtime
    │  1. Start InMemorySpanExporter
    │  2. Instrument with GoogleADKInstrumentor
    │  3. Execute ADK agent(s)
    │  4. Collect spans → Build Trajectory Graph
    │  5. Return { result, trajectory }
    ▼
API Layer
    │  Forward response
    ▼
UI
    │  Render nodes + edges as flow diagram
```

---

## 4. Step-by-Step Implementation Plan

### Phase 1 – Agent Runtime (gRPC Server)

#### Step 1.1 – Install Required Packages

```bash
pip install google-adk
pip install openinference-instrumentation-google-adk
pip install opentelemetry-sdk opentelemetry-api
```

#### Step 1.2 – Create the Dry-Run Tracer Helper

Create a module `dry_run_tracer.py`.

Responsibilities:

- Create an `InMemorySpanExporter`
- Create a `TracerProvider` + `SimpleSpanProcessor`
- Instrument with `GoogleADKInstrumentor`
- Provide a context manager that activates the tracer only for the duration of the dry run
- On exit: force flush and expose the finished spans

Key behaviour:

- Completely request-scoped
- Does not affect production tracing
- Zero external dependencies beyond OpenTelemetry

#### Step 1.3 – Build the Trajectory Graph Converter

Create a module `trajectory_builder.py`.

Input: list of `ReadableSpan` objects collected by the InMemorySpanExporter.

For every span create a **Node** containing:

- `id` (prefer `graph.node.id` if present, otherwise use span_id)
- `label` (agent name / tool name / span name)
- `kind` (`openinference.span.kind` → `AGENT` | `LLM` | `TOOL` | `CHAIN`)
- `input` and `output` (from `input.value` / `output.value`)
- `duration_ms`, `status`, and useful extra attributes
- `depth` (for layout)

For every parent-child relationship create an **Edge**:

- `source` → parent node id
- `target` → child node id

Return the clean structure:

```json
{
  "nodes": [...],
  "edges": [...],
  "root_ids": [...]
}
```

#### Step 1.4 – Implement the gRPC DryRun Method

Define the RPC:

```protobuf
rpc DryRun(DryRunRequest) returns (DryRunResponse);
```

Inside the method:

1. Enter the dry-run tracer context manager.
2. Execute the ADK agent or multi-agent graph with the given input.
3. Collect the finished spans from the InMemorySpanExporter.
4. Call `build_trajectory_graph(spans)`.
5. Return both the normal agent `result` and the `trajectory` graph in the gRPC response.

---

### Phase 2 – API Layer

#### Step 2.1 – Expose HTTP Endpoint

```
POST /api/v1/agents/{agent_id}/dry-run
```

#### Step 2.2 – Forward to gRPC

- Receive the dry-run request from the UI.
- Call the gRPC `DryRun` method.
- Return the exact response received from the runtime:

```json
{
  "result": { ... },
  "trajectory": {
    "nodes": [...],
    "edges": [...],
    "root_ids": [...]
  }
}
```

No extra storage or transformation is required. The trajectory remains purely request-scoped.

---

### Phase 3 – Visual Builder UI

#### Step 3.1 – Trigger Dry Run

When the user clicks “Dry Run” in the visual builder:

- Call the API endpoint.
- Show a loading state while the agent is running.

#### Step 3.2 – Receive Trajectory Graph

On successful response:

- Extract `trajectory.nodes` and `trajectory.edges`.
- Feed them into the graph library (React Flow / xyflow recommended).

#### Step 3.3 – Render the Flow Diagram

- Each **Node** becomes a visual card.
  - Color by `kind`:
    - `AGENT` → blue
    - `LLM` → purple
    - `TOOL` → green
    - Error → red border
  - Show short label + duration.
- Each **Edge** becomes an arrow showing the real parent → child relationship.
- On node click → open a side panel that displays the full `input` and `output`.
- Use auto-layout (dagre or elk) for a clean top-to-bottom or left-to-right flow.

#### Step 3.4 – Ephemeral Lifecycle

- Keep the graph only in React state (or equivalent).
- When the user closes the dry-run tab or refreshes the page, the data is discarded.
- No backend storage is required.

---

## 5. Final Data Contract

```json
{
  "result": {
    // normal ADK agent output
  },
  "trajectory": {
    "nodes": [
      {
        "id": "research_agent",
        "label": "Research Agent",
        "kind": "AGENT",
        "status": "OK",
        "duration_ms": 1840,
        "input": { "...": "..." },
        "output": { "...": "..." },
        "depth": 1
      },
      {
        "id": "call_llm_1",
        "label": "call_llm",
        "kind": "LLM",
        "status": "OK",
        "duration_ms": 920,
        "input": { "...": "..." },
        "output": { "...": "..." },
        "depth": 2
      }
    ],
    "edges": [
      {
        "id": "e1",
        "source": "orchestrator",
        "target": "research_agent"
      },
      {
        "id": "e2",
        "source": "research_agent",
        "target": "call_llm_1"
      }
    ],
    "root_ids": ["orchestrator"]
  }
}
```

---

## 6. Recommended Implementation Order

1. **Backend first**
   - Implement dry-run tracer + trajectory builder.
   - Implement gRPC `DryRun` method that returns the graph.
2. **API layer**
   - Simple proxy endpoint that forwards the gRPC response.
3. **UI**
   - Call the endpoint and render nodes + edges.
4. **Polish**
   - Node colours, side panel for input/output, loading states, error handling.

---

## 7. Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Persistence | None (request-scoped) | Matches “show for some time and done” |
| Span collection | `InMemorySpanExporter` | Zero external dependency, instant |
| Graph format | nodes + edges | Directly usable by React Flow and similar libraries |
| Instrumentation | `GoogleADKInstrumentor` | Provides clean `openinference.span.kind`, `input.value`, `output.value` |
| Transport | Trajectory travels inside the gRPC response | No extra API or storage needed |

---

## 8. Example Visual Result

```
┌──────────────────────────┐
│     invoke_agent         │  ← Root (dry-run entry point)
│     (orchestrator)       │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   research_agent         │  ← Sub-agent 1
│   input: "Find latest.." │
│   output: "..."          │
└────────────┬─────────────┘
             │
      ┌──────┴──────┐
      ▼             ▼
┌───────────┐  ┌──────────────┐
│  call_llm │  │ execute_tool │
└─────┬─────┘  └──────┬───────┘
      │               │
      └───────┬───────┘
              │
              ▼
┌──────────────────────────┐
│   analysis_agent         │  ← Sub-agent 2
│   input: "Summarize.."   │
│   output: "..."          │
└──────────────────────────┘
```

Every sub-agent invocation appears as an `AGENT` node connected by an edge from its parent. LLM calls and tool calls appear as child nodes under the sub-agent that invoked them.

---

## 9. Summary

This plan delivers a complete, lightweight, near real-time dry-run trajectory experience for Google ADK agents:

- No database
- No external observability dependency for the dry-run path
- Clean nodes + edges graph that maps directly to a visual flow diagram
- Full visibility of input and output for every sub-agent, LLM call, and tool call

The implementation is intentionally minimal and request-scoped so that the visual builder remains fast and simple.
