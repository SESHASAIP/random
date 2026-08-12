# Google ADK Dry-Run Agent Trajectory Visualization
## End-to-End Implementation Plan

**Goal**  
Enable a visual builder dry-run experience that shows the full agent execution trajectory (sub-agents, LLM calls, tool calls) as an interactive **nodes + edges graph** in the UI — near real-time, ephemeral, with zero database dependency for the dry-run path.

---

## 1. Core Terminology

| Term | Meaning |
|------|---------|
| **Dry Run** | A one-off execution of an agent (or multi-agent workflow) triggered from the visual builder for inspection purposes. |
| **Span** | A single unit of work captured by OpenTelemetry (e.g. one agent invocation, one LLM call, one tool execution). |
| **Trace** | The complete tree of spans that share the same `trace_id` for one dry-run. |
| **Trajectory** | The structured representation of the entire execution path derived from the spans. |
| **Nodes + Edges Graph** | The final data structure returned to the UI: a list of nodes and a list of edges that can be rendered by React Flow (or similar). |
| **In-Memory Collector** | OpenTelemetry `InMemorySpanExporter` used only during a dry-run. Spans live only for the lifetime of the request. |
| **OpenInference** | Semantic conventions + instrumentor that enrich ADK spans with `openinference.span.kind`, `input.value`, `output.value`, etc. |
| **Sub-agent** | Any agent that is invoked by a parent agent / orchestrator. Appears as an `AGENT` (or `CHAIN`) node in the graph. |
| **Request-scoped** | Data exists only for the duration of one dry-run request and is discarded afterwards. |

---

## 2. High-Level Architecture

```
┌─────────────┐       HTTP        ┌─────────────┐       gRPC        ┌──────────────────────┐
│  UI Repo    │ ───────────────► │  API Repo   │ ───────────────► │  Agent Runtime      │
│ (Visual     │                  │ (HTTP       │                  │  (gRPC Server)      │
│  Builder)   │ ◄─────────────── │  routes)    │ ◄─────────────── │                     │
└─────────────┘   {result,       └─────────────┘   DryRunResponse  │  - ADK Agents      │
                  trajectory}                      {result,        │  - InMemory Collector│
                                                   trajectory}     │  - Graph Builder    │
                                                                   └──────────────────────┘
```

**Key principle**: The trajectory is built inside the Agent Runtime and travels back inside the same response. No intermediate storage.

---

## 3. End-to-End Implementation Steps

### Step 1 — Instrument Google ADK with OpenInference

In the Agent Runtime (gRPC server process):

```python
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, InMemorySpanExporter

# Create a dedicated in-memory exporter for dry-runs
in_memory_exporter = InMemorySpanExporter()

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))

# Instrument ADK
GoogleADKInstrumentor().instrument(tracer_provider=provider)
```

**Why OpenInference?**  
It guarantees the presence of:
- `openinference.span.kind` → `AGENT` | `LLM` | `TOOL` | `CHAIN`
- `input.value` / `output.value`
- Clean hierarchical parent-child relationships

---

### Step 2 — Create a Dry-Run Context Manager

```python
from contextlib import contextmanager
from opentelemetry import trace
import uuid

@contextmanager
def dry_run_tracing():
    """
    Activates in-memory span collection for the duration of one dry-run.
    Restores the previous tracer provider on exit.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous_provider = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)

    run_id = str(uuid.uuid4())

    try:
        yield {
            "run_id": run_id,
            "exporter": exporter,
            "provider": provider,
        }
    finally:
        provider.force_flush()
        trace.set_tracer_provider(previous_provider)
```

---

### Step 3 — Build the Nodes + Edges Graph

After the agent finishes, convert the collected spans into a UI-ready graph.

```python
from typing import List, Dict, Any
from collections import defaultdict
from opentelemetry.sdk.trace import ReadableSpan
import json

def _safe_parse(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value

def build_trajectory_graph(spans: List[ReadableSpan]) -> Dict[str, Any]:
    """
    Converts a list of OpenTelemetry spans into a nodes + edges graph.
    """
    if not spans:
        return {"nodes": [], "edges": [], "root_ids": [], "span_count": 0}

    def get_node_id(span: ReadableSpan) -> str:
        attrs = span.attributes or {}
        # Prefer explicit graph node id if present, otherwise use span_id
        return str(attrs.get("graph.node.id") or format(span.context.span_id, "016x"))

    span_by_otel_id = {span.context.span_id: span for span in spans}
    display_id = {span.context.span_id: get_node_id(span) for span in spans}

    children = defaultdict(list)
    parent_of = {}
    roots = []

    for span in spans:
        sid = span.context.span_id
        parent_sid = span.parent.span_id if span.parent else None

        if parent_sid and parent_sid in span_by_otel_id:
            children[parent_sid].append(sid)
            parent_of[sid] = parent_sid
        else:
            roots.append(sid)

    # ----- Nodes -----
    nodes = []
    for span in spans:
        attrs = dict(span.attributes or {})
        nid = get_node_id(span)

        kind = attrs.get("openinference.span.kind", "UNKNOWN")
        label = (
            attrs.get("graph.node.name")
            or attrs.get("agent.name")
            or attrs.get("tool.name")
            or span.name
        )

        nodes.append({
            "id": nid,
            "label": label,
            "kind": kind,                          # AGENT | LLM | TOOL | CHAIN
            "status": span.status.status_code.name if span.status else "UNSET",
            "duration_ms": round((span.end_time - span.start_time) / 1_000_000, 1)
                           if span.end_time and span.start_time else None,
            "input": _safe_parse(attrs.get("input.value")),
            "output": _safe_parse(attrs.get("output.value")),
            "start_time": span.start_time,
            "data": {k: v for k, v in attrs.items()
                     if k not in {
                         "input.value", "output.value",
                         "openinference.span.kind",
                         "graph.node.id", "graph.node.parent_id", "graph.node.name"
                     }}
        })

    # ----- Edges -----
    edges = []
    for idx, (child_sid, parent_sid) in enumerate(parent_of.items()):
        edges.append({
            "id": f"e{idx}",
            "source": display_id[parent_sid],
            "target": display_id[child_sid],
        })

    # ----- Depth (helpful for layout) -----
    depth_map = {}
    def assign_depth(sid, depth=0):
        depth_map[display_id[sid]] = depth
        for child in children[sid]:
            assign_depth(child, depth + 1)

    for root in roots:
        assign_depth(root)

    for node in nodes:
        node["depth"] = depth_map.get(node["id"], 0)

    nodes.sort(key=lambda n: (n["depth"], n.get("start_time") or 0))

    return {
        "nodes": nodes,
        "edges": edges,
        "root_ids": [display_id[r] for r in roots],
        "span_count": len(spans),
    }
```

---

### Step 4 — Wire into the gRPC Dry-Run Handler

```python
# Inside your gRPC service method for dry-run
async def DryRun(self, request, context):
    with dry_run_tracing() as ctx:
        # Execute the ADK agent / workflow
        result = await self.runner.run(
            agent=request.agent_config,
            input=request.input_data,
            # ... any other ADK parameters
        )

        # Collect spans and build graph
        finished_spans = ctx["exporter"].get_finished_spans()
        trajectory = build_trajectory_graph(finished_spans)

    return DryRunResponse(
        run_id=ctx["run_id"],
        result=result,
        trajectory=trajectory,          # { nodes, edges, root_ids, span_count }
    )
```

---

### Step 5 — API Layer (HTTP)

The API repo simply proxies the gRPC response:

```python
@router.post("/dry-run")
async def dry_run(payload: DryRunRequest):
    grpc_response = await grpc_client.DryRun(payload)

    return {
        "run_id": grpc_response.run_id,
        "result": grpc_response.result,
        "trajectory": grpc_response.trajectory,   # ready for the UI
    }
```

---

### Step 6 — UI Rendering (Visual Builder)

In the UI repo, after calling the dry-run endpoint:

```ts
const { trajectory } = await api.dryRun(payload);

// trajectory = { nodes: [...], edges: [...], root_ids: [...] }

<ReactFlow
  nodes={trajectory.nodes.map(n => ({
    id: n.id,
    type: n.kind.toLowerCase(),          // custom node types per kind
    data: {
      label: n.label,
      kind: n.kind,
      input: n.input,
      output: n.output,
      duration_ms: n.duration_ms,
      status: n.status,
      ...n.data,
    },
    position: { x: 0, y: 0 },            // or use dagre/elk auto-layout
  }))}
  edges={trajectory.edges}
  fitView
/>
```

**Recommended node colors**
- `AGENT` / `CHAIN` → blue
- `LLM` → purple / orange
- `TOOL` → green
- Error status → red border

**Side panel on node click**
- Show full `input` and `output`
- Duration, model name, tool name, status

---

## 4. Expected Visual Result

```
┌──────────────────────────┐
│     invoke_agent         │  ← Root
│     (orchestrator)       │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   research_agent         │  ← Sub-agent 1
│   input → output         │
└────────────┬─────────────┘
             │
      ┌──────┴──────┐
      ▼             ▼
┌───────────┐  ┌──────────────┐
│  call_llm │  │ execute_tool │
└───────────┘  └──────────────┘
             │
             ▼
┌──────────────────────────┐
│   analysis_agent         │  ← Sub-agent 2
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   writer_agent           │  ← Sub-agent 3
└──────────────────────────┘
```

Every sub-agent invocation appears as its own `AGENT` node connected by an edge from its parent.

---

## 5. Design Decisions (Why This Approach)

| Decision | Reason |
|----------|--------|
| In-memory only (no DB) | Matches “show for some time and done”. Zero persistence overhead. |
| Request-scoped | Trajectory lives only for the lifetime of the dry-run request. |
| Nodes + Edges | Universal format accepted by React Flow, Cytoscape, D3, etc. |
| OpenInference instrumentor | Gives clean `kind`, `input`, `output` attributes out of the box for ADK. |
| Graph built in Agent Runtime | Keeps the conversion logic next to the spans; API and UI stay simple. |
| No Neo4j / external graph DB | Would add heavy weight and persistence that is not needed for dry-runs. |

---

## 6. Optional Future Enhancements (Still Lightweight)

- Auto-layout with `dagre` or `elkjs` on the frontend.
- Prune very low-level spans if the graph becomes too noisy.
- Short-lived in-memory cache (or Redis with 5–10 min TTL) keyed by `run_id` if users need to re-open a dry-run after a page refresh.
- Stream intermediate progress events while the agent is still running; send the final trajectory as the last message.

---

## 7. Implementation Checklist

- [ ] Add `openinference-instrumentation-google-adk` to the Agent Runtime
- [ ] Implement `dry_run_tracing` context manager
- [ ] Implement `build_trajectory_graph`
- [ ] Extend the gRPC `DryRun` response to include `trajectory`
- [ ] Forward `trajectory` from API to UI
- [ ] Render nodes + edges with React Flow (or equivalent)
- [ ] Add node click → details panel (input / output)
- [ ] Color nodes by `kind`
- [ ] Test with a multi-agent ADK workflow that has at least two sub-agents

---

## 8. Summary

This plan delivers a clean, production-grade dry-run trajectory visualization for Google ADK agents:

- Fully ephemeral
- Near real-time
- No database required for the dry-run path
- Uses the same OpenTelemetry + OpenInference foundation that ADK already supports
- Produces a standard nodes + edges graph that any modern frontend graph library can render

The entire trajectory is captured, converted, and returned in a single request-response cycle.