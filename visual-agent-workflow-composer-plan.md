# Visual Agent Workflow Composer

## Core Product Idea

We are building a **Visual Agent Workflow Composer**.

A UI where users can:

1. See agents that already exist in the registry (database)
2. Visually arrange those agents into a workflow
3. Configure how data moves between agents
4. Dry-run (simulate) the complete workflow
5. Save the workflow back to the database

This is **not** about creating new agents from scratch in the UI.  
It is about **composing existing agents** into reusable flows.

---

## One-line Summary

A visual tool to design, test, and save multi-agent workflows using agents that already live in the registry.

---

## Implementation Plan (Product First)

### Phase 1: Core User Experience (Foundation)

**Goal:** Deliver the main user journey end-to-end, even if features are basic.

**What the user can do:**
- See a list of available agents (with name + short capability description)
- Drag agents onto a canvas
- Connect agents with arrows to define the flow
- Click **Dry Run** and see step-by-step execution
- Click **Save** and store the workflow

**UI Layout:**
- **Left panel** → Agent catalog (from registry)
- **Center** → Canvas (drag & drop + connections)
- **Right panel** → Configuration of the selected node
- **Top bar** → Dry Run + Save buttons
- **Bottom / side panel** → Dry-run results and traces

**Minimum technical pieces required:**
- API to list agents from the existing registry
- Ability to save a simple workflow graph (nodes + edges)
- Basic sequential dry-run execution
- Simple event stream so the UI can show which agent is currently running

**Outcome of Phase 1:**  
The actual product idea is working.

---

### Phase 2: Make the Flow Real

**Goal:** Support the workflow patterns users actually need.

**Add support for:**
- Conditional branching (if / else)
- Parallel execution of agents
- Data mapping between agents (output of one agent → input of the next)
- Graph validation (prevent saving broken flows)

**Technical focus:**
- Richer graph model
- Dry-run engine that can handle conditions and parallel paths
- Clear validation errors shown directly on the canvas

---

### Phase 3: Production-Ready Experience

**Goal:** Make the tool usable for real work.

**Add:**
- Workflow versioning
- Detailed dry-run traces (inputs, outputs, timing, errors)
- Ability to reopen and edit previously saved workflows
- Human-in-the-loop nodes (optional approval steps)
- Basic permissions (who can create / edit workflows)

---

### Phase 4: Polish & Scale

- Search and filter agents by capability
- Workflow templates / starter flows
- Collaboration features (comments, sharing)
- Performance improvements for large graphs
- Audit logs

---

## Guiding Principle

Every technical decision must answer this question:

> Does this help the user **visually create, test, and save** an agent workflow?

If the answer is not clearly yes, the work is de-prioritized.

---

## Next Step

Expand **Phase 1** in detail:
- Exact screen layout and interactions
- Minimum set of APIs required
- Data shapes for the graph that the frontend will send/receive
