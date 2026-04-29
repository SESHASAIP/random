# Society-of-Agents: Structured Handoff + YAML Skills — Implementation Plan

## Read This First

We are building three things that work together:

1. **Structured Handoff Object** — a Pydantic schema that replaces prose summaries at society boundaries
2. **Handoff extraction via ADK `output_schema`** — when the router says DONE, the orchestrator makes one call to an LlmAgent with `output_schema=StructuredHandoff`. ADK enforces the schema natively. Zero custom parsing.
3. **YAML Skill Loader** — agents defined as YAML files in a folder, hot-reloaded at runtime without restart

Here is how they connect:

```
YAML files in ./skills/
        │
        ▼
  SkillLoader reads YAMLs → builds LlmAgents → registers in SpecialistRegistry
        │
        ▼
  TaskEngineOrchestrator runs the society-of-agents loop
        │
        ▼
  Router picks agents each round (unchanged, no output_schema on it)
        │
        ▼
  Router says DONE → orchestrator runs one LlmAgent with output_schema=StructuredHandoff
  ADK enforces the schema. Guaranteed valid Pydantic output. No custom parsing.
        │
        ▼
  Handoff stored in session.state → parent reads structured fields
```

Why this is better than hacking the router prompt to output JSON:
- ADK validates the output against the Pydantic schema automatically
- No markdown stripping, no JSON parsing, no retry-on-parse-failure code
- output_key auto-stores the result in session.state
- The router stays simple — it just routes. It doesn't do double duty.

---

## Build Order

```
Part 1: handoff.py          — the Pydantic schema (no dependencies)
Part 2: orchestrator.py     — add handoff extraction LlmAgent, called after router says DONE
Part 3: society_of_mind.py  — read handoff from session state, no Summarizer
Part 4: skill_loader.py     — YAML loading + hot-reload
Part 5: main.py             — wiring
```

---

## Part 1: Structured Handoff Object

### What you are building

A Pydantic model that defines the exact shape of data that flows upward from a child society to its parent. ADK uses this as the `output_schema` to enforce structured output.

### Why this matters

Without this, summaries are free-form prose and the parent loses ~60% of information. With a Pydantic schema enforced by ADK, the output is guaranteed to be valid structured data. No parsing code needed.

### Build instructions

Create file: `handoff.py`

**The sub-models:**

```python
from pydantic import BaseModel, Field
from enum import Enum


class HandoffStatus(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_CAVEATS = "completed_with_caveats"
    FAILED = "failed"
    NEEDS_MORE_WORK = "needs_more_work"


class Decision(BaseModel):
    text: str          # what was decided
    confidence: float  # 0.0 to 1.0
    reasoning: str     # one sentence why


class Fact(BaseModel):
    text: str          # the factual finding
    source: str        # which agent found this
    verified: bool     # was this cross-checked by another agent


class OpenQuestion(BaseModel):
    text: str          # what is unresolved
    blocking: bool     # does this block progress


class DissentingOpinion(BaseModel):
    text: str          # the disagreement
    raised_by: str     # which agent raised it
    severity: str      # "critical" / "high" / "medium" / "low"
```

**The main model:**

```python
class StructuredHandoff(BaseModel):
    verdict: str = Field(
        description="One sentence summary of the outcome."
    )
    status: HandoffStatus = Field(
        description="completed, completed_with_caveats, failed, or needs_more_work"
    )
    decisions: list[Decision] = Field(
        default_factory=list,
        description="Every decision made during the internal debate."
    )
    facts: list[Fact] = Field(
        default_factory=list,
        description="Every factual finding. verified=true only if cross-checked."
    )
    artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="Named outputs. Key is name, value is FULL content. Do NOT summarize."
    )
    open_questions: list[OpenQuestion] = Field(
        default_factory=list,
        description="Anything the society could not resolve."
    )
    dissenting_opinions: list[DissentingOpinion] = Field(
        default_factory=list,
        description="Disagreements not fully resolved."
    )
    rounds_taken: int = Field(default=0, description="Internal rounds completed.")
    tokens_consumed: int = Field(default=0, description="Total tokens used.")
```

**Add `to_context_string()` method:**

Converts the handoff into readable text for injection into agent prompts via `{conversation_context}`.

```python
def to_context_string(self) -> str:
    parts = [f"VERDICT: {self.verdict}", f"STATUS: {self.status.value}"]

    if self.decisions:
        parts.append("DECISIONS:")
        for d in self.decisions:
            parts.append(f"  - {d.text} (confidence: {d.confidence}, reason: {d.reasoning})")

    if self.facts:
        parts.append("FACTS:")
        for f in self.facts:
            tag = "verified" if f.verified else "unverified"
            parts.append(f"  - {f.text} [{tag}, source: {f.source}]")

    if self.artifacts:
        parts.append("ARTIFACTS PRODUCED:")
        for name, content in self.artifacts.items():
            preview = content[:500] + "..." if len(content) > 500 else content
            parts.append(f"  {name}: {preview}")

    if self.open_questions:
        parts.append("OPEN QUESTIONS:")
        for q in self.open_questions:
            tag = "BLOCKING" if q.blocking else "non-blocking"
            parts.append(f"  - {q.text} [{tag}]")

    if self.dissenting_opinions:
        parts.append("DISSENTING OPINIONS:")
        for d in self.dissenting_opinions:
            parts.append(f"  - [{d.severity.upper()}] {d.text} (raised by: {d.raised_by})")

    return "\n".join(parts)
```

**Add `error_handoff()` class method:**

```python
@classmethod
def error_handoff(cls, error_message: str, rounds_taken: int = 0) -> "StructuredHandoff":
    return cls(
        verdict=f"Society failed: {error_message}",
        status=HandoffStatus.FAILED,
        rounds_taken=rounds_taken,
    )
```

### Done criteria

- All Pydantic models defined with Field descriptions (ADK uses these in the prompt)
- `to_context_string()` produces readable text
- `error_handoff()` creates failure handoffs
- All fields have defaults so partial handoffs are valid
- Model is compatible with ADK's `output_schema` parameter

---

## Part 2: Handoff Extraction in Orchestrator

### What you are building

An `LlmAgent` inside the orchestrator with `output_schema=StructuredHandoff` that runs ONCE after the router says DONE. This agent reads the conversation log and produces a guaranteed-valid structured handoff. ADK enforces the schema — no custom parsing needed.

### Why this matters

The router stays simple — it just routes. After it says DONE, the orchestrator runs one more LlmAgent call with `output_schema=StructuredHandoff`. ADK guarantees the output matches the Pydantic schema. All the custom JSON parsing, markdown stripping, and retry logic from the previous plan is gone. ADK handles it.

### Build instructions

Modify file: `orchestrator.py`

**Change 1: Create the handoff extractor agent in the constructor**

Inside `TaskEngineOrchestrator.__init__`:

```python
from handoff import StructuredHandoff

class TaskEngineOrchestrator(BaseAgent):
    def __init__(self, name, registry, router_model="gemini-2.5-flash",
                 max_rounds=10, token_budget=100_000, human_review_after=None, **kwargs):

        # ... existing setup (router, context_manager, convergence_detector) ...

        # Handoff extractor — runs once after router says DONE
        self.handoff_extractor = LlmAgent(
            name=f"{name}_handoff_extractor",
            model=router_model,  # same model as router, no extra model needed
            instruction="""You are a structured data extractor.

Read the conversation log from a completed society-of-agents debate and extract information into the required schema.

TASK: {task}

CONVERSATION LOG:
{conversation_log_text}

EXTRACTION RULES:
1. Extract EVERY decision made, not just the final one.
2. Extract EVERY factual finding. Set verified=true only if a second agent confirmed it.
3. Include the FULL content of any produced artifact (draft, report, code) in artifacts. Do NOT summarize artifacts — include them complete.
4. If any agent disagreed and it was not resolved, include it in dissenting_opinions.
5. If there are open questions, include them. Set blocking=true if they prevent the task from being complete.
6. Set status to:
   - "completed" if work is done and approved
   - "completed_with_caveats" if done but with unresolved concerns
   - "failed" if the society could not produce useful output
   - "needs_more_work" if ran out of rounds before finishing""",
            output_schema=StructuredHandoff,  # ADK enforces this
            output_key="handoff",             # auto-stored in session.state["handoff"]
        )

        super().__init__(
            name=name,
            sub_agents=registry.all_agent_instances() + [self.handoff_extractor],
            **kwargs
        )
```

The `handoff_extractor` is NOT a specialist agent. It is NOT registered in the SpecialistRegistry. The router never routes to it. The orchestrator calls it directly after the loop ends.

**Change 2: Update `_run_async_impl` to call the extractor after DONE**

The router and main loop stay EXACTLY as they are. No changes to routing logic, routing prompt, or RoutingDecision dataclass.

After the main loop ends (for ANY reason — DONE, convergence, budget, error), add the handoff extraction step:

```python
async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator:

    # ... existing session load ...
    # ... existing main loop (completely unchanged) ...
    # ... existing final_output logic ...

    # ===== NEW: Handoff extraction step =====
    # This runs ONCE after the loop ends, regardless of how it ended.

    # Build the conversation log text for the extractor
    log_text = "\n\n".join(
        f"[Round {e['round_number']} | {e['agent_name']} | status={e['status']}]:\n{e['output']}"
        for e in session.conversation_log
        if e.get("status") in ("success", "partial")
    )
    ctx.session.state["conversation_log_text"] = log_text
    ctx.session.state["task"] = session.task

    # Run the handoff extractor
    try:
        async for event in self.handoff_extractor.run_async(ctx):
            pass  # we don't yield these events — handoff is internal bookkeeping

        # ADK stored the result in session.state["handoff"] via output_key
        # Parse it into a StructuredHandoff to add metadata
        handoff_data = ctx.session.state.get("handoff")
        if handoff_data:
            if isinstance(handoff_data, str):
                import json
                handoff_data = json.loads(handoff_data)
            handoff = StructuredHandoff.model_validate(handoff_data)
            handoff.rounds_taken = session.total_rounds
            handoff.tokens_consumed = session.total_tokens_used
            ctx.session.state["handoff"] = handoff.model_dump()
            ctx.session.state["handoff_text"] = handoff.to_context_string()

            # Update final_output from handoff artifacts if available
            if handoff.artifacts:
                first_key = next(iter(handoff.artifacts))
                session.final_output = handoff.artifacts[first_key]
            elif not session.final_output:
                session.final_output = handoff.verdict

    except Exception as e:
        # Extractor failed — build fallback handoff
        logger.warning(f"Handoff extraction failed: {e}")
        last_output = ""
        for entry in reversed(session.conversation_log):
            if entry.get("status") in ("success", "partial"):
                last_output = entry.get("output", "")
                break

        status_map = {
            SessionStatus.COMPLETED: HandoffStatus.COMPLETED_WITH_CAVEATS,
            SessionStatus.TERMINATED_CONVERGENCE: HandoffStatus.COMPLETED_WITH_CAVEATS,
            SessionStatus.TERMINATED_BUDGET: HandoffStatus.NEEDS_MORE_WORK,
            SessionStatus.TERMINATED_ERROR: HandoffStatus.FAILED,
            SessionStatus.PAUSED_FOR_HUMAN: HandoffStatus.NEEDS_MORE_WORK,
        }

        fallback = StructuredHandoff(
            verdict=f"Handoff extraction failed: {str(e)}. Raw output preserved in artifacts.",
            status=status_map.get(session.session_status, HandoffStatus.NEEDS_MORE_WORK),
            artifacts={"raw_output": last_output} if last_output else {},
            rounds_taken=session.total_rounds,
            tokens_consumed=session.total_tokens_used,
        )
        ctx.session.state["handoff"] = fallback.model_dump()
        ctx.session.state["handoff_text"] = fallback.to_context_string()
        if not session.final_output:
            session.final_output = last_output or fallback.verdict

    self._save_session(ctx, session)
```

**What stays unchanged:**
- TaskRouter — no changes to routing prompt, response format, or parsing
- RoutingDecision — no new fields
- ConversationContextManager — unchanged
- ConvergenceDetector — unchanged
- AgentExecutor — unchanged
- The entire main loop — unchanged

**What is new:**
- One `LlmAgent` (`handoff_extractor`) created in the constructor with `output_schema=StructuredHandoff`
- One block of code after the main loop that runs the extractor and handles failure

### How ADK's output_schema works here

When `handoff_extractor.run_async(ctx)` runs:

1. ADK automatically incorporates the StructuredHandoff schema into the system instruction
2. The LLM generates a JSON response matching the schema
3. ADK validates the response against the Pydantic model
4. If validation passes, the result is stored in `session.state["handoff"]` via `output_key`
5. If validation fails, ADK raises a `pydantic.ValidationError`

We catch the validation error in the try/except and fall back to a minimal handoff. No custom JSON parsing. No markdown stripping. No retry logic. ADK does the work.

### Done criteria

- `handoff_extractor` is an LlmAgent with `output_schema=StructuredHandoff` and `output_key="handoff"`
- It is created in the constructor but NOT registered in the SpecialistRegistry
- It is included in `sub_agents` (required by ADK) but the router never routes to it
- It runs exactly ONCE, after the main loop ends
- The conversation log text is injected into session state before running the extractor
- Fallback handoff is produced if extraction fails
- Round count and token count are populated on the handoff
- The main loop, router, and all other components are UNCHANGED

---

## Part 3: Update SocietyOfMind

### What you are building

Updating SocietyOfMindAgent to read the handoff from session state instead of running a Summarizer. The inner orchestrator already produced the handoff in Part 2.

### Why this matters

No Summarizer agent. The inner orchestrator's handoff extractor already ran and stored the result in `session.state["handoff"]`. The SocietyOfMind just reads it and re-stores it under its own name for the parent.

### Build instructions

Modify file: `society_of_mind.py`

**Remove:** `self.summarizer` and all summarizer code.

**Updated constructor:**

```python
class SocietyOfMindAgent(BaseAgent):
    def __init__(self, name, registry, router_model="gemini-2.5-flash",
                 max_rounds=6, token_budget=50_000, description=""):
        self.inner_engine = TaskEngineOrchestrator(
            name=f"{name}_inner_engine",
            registry=registry,
            router_model=router_model,
            max_rounds=max_rounds,
            token_budget=token_budget
        )
        super().__init__(
            name=name,
            description=description,
            sub_agents=[self.inner_engine],
        )
```

**Updated `_run_async_impl`:**

```python
async def _run_async_impl(self, ctx):
    # Step 1: Run internal debate (swallow events)
    # The inner engine's handoff_extractor runs as part of this
    async for event in self.inner_engine.run_async(ctx):
        pass

    # Step 2: Read handoff from session state
    # The inner orchestrator already stored it via handoff_extractor
    handoff_data = ctx.session.state.get("handoff")
    if handoff_data:
        if isinstance(handoff_data, str):
            import json
            handoff_data = json.loads(handoff_data)
        handoff = StructuredHandoff.model_validate(handoff_data)
    else:
        handoff = StructuredHandoff.error_handoff(
            "Inner society produced no handoff",
            rounds_taken=ctx.session.state.get("task_session", {}).get("total_rounds", 0)
        )

    # Step 3: Re-store under THIS society's name for the parent
    ctx.session.state[f"{self.name}_handoff"] = handoff.model_dump()
    ctx.session.state[f"latest_{self.name.lower()}_output"] = handoff.to_context_string()

    # Step 4: Store primary artifact as output
    if handoff.artifacts:
        first_key = next(iter(handoff.artifacts))
        ctx.session.state[f"{self.name}_output"] = handoff.artifacts[first_key]
    else:
        ctx.session.state[f"{self.name}_output"] = handoff.verdict
```

### Nested rollup flow

```
Depth 2: FactChecker's inner orchestrator finishes
         → handoff_extractor runs with output_schema=StructuredHandoff
         → ADK validates and stores in session.state["handoff"]
         → SocietyOfMind re-stores as latest_factchecker_output (text)

Depth 1: ResearchSociety sees FactChecker's text in conversation log
         → ResearchSociety's loop finishes
         → handoff_extractor reads the full log (includes FactChecker's text)
         → produces its OWN handoff that rolls up FactChecker's findings
         → SocietyOfMind re-stores as latest_researchsociety_output

Depth 0: Top-level sees latest_researchsociety_output as one text block
         Knows nothing about FactChecker, SourceValidator, BiasDetector
```

Each level's handoff_extractor sees the text output from child societies in the conversation log. It extracts and rolls up facts, decisions, and artifacts from those text outputs into its own structured handoff. The parent never sees below its direct children.

### Done criteria

- No Summarizer agent in SocietyOfMind
- Reads handoff from `session.state["handoff"]` (produced by inner orchestrator's extractor)
- Re-stores under `{self.name}_handoff` (dict) and `latest_{self.name.lower()}_output` (text)
- Error handoff if inner engine produced nothing
- Nested rollup works: child handoff text flows through conversation log to parent's extractor

---

## Part 4: YAML Skill Loader

### What you are building

YAML files → agents registered in SpecialistRegistry. Hot-reload via daemon thread.

### The YAML format

```yaml
# Required
name: Researcher
model: gemini-2.5-flash
description: >
  Gathers facts and background context.
  Good first step when the team needs foundational information.
instruction: |
  You are a thorough technical researcher.
  TASK: {task}
  CONTEXT: {conversation_context}
  Provide factual findings.
output_key: researcher_output
capabilities:
  - research
  - data_gathering

# Optional (defaults applied if missing)
max_retries: 2          # default: 2
timeout_seconds: 45     # default: 60.0
priority: 1             # default: 0
```

Required fields: `name`, `model`, `description`, `instruction`, `output_key`, `capabilities`

Rules:
- `name` must be non-empty string, unique across all skill files
- `capabilities` must be non-empty list
- `instruction` MUST contain `{task}` and `{conversation_context}`
- `output_key` must be unique per agent

### Build instructions

Create file: `skill_loader.py`

**Thing 1: `parse_skill_yaml(path) -> SpecialistEntry`**

```
1. Read with yaml.safe_load() → raise SkillParseError on failure
2. Check result is a dict
3. Validate required fields: name, model, description, instruction, output_key, capabilities
   If missing → raise SkillParseError listing which ones
4. Validate: name is non-empty string, capabilities is non-empty list
5. Apply defaults for missing optionals: max_retries=2, timeout_seconds=60.0, priority=0
6. Create LlmAgent(name, model, description, instruction, output_key)
7. Create and return SpecialistEntry(agent, description, capabilities, max_retries, timeout_seconds, priority)
```

Also create: `SkillParseError(Exception)` and `_file_hash(path) -> str` (MD5 hex digest).

**Thing 2: SkillLoader class**

```python
class SkillLoader:
    def __init__(self, skills_dir, registry, on_change=None):
        self._file_hashes = {}      # filename → MD5
        self._file_to_agent = {}    # filename → agent name
        self._stop_event = threading.Event()
        self._watcher_thread = None
```

**`load_all()`** — glob *.yaml and *.yml, call `_load_single()` for each, return results dict.

**`_load_single(yaml_path)`** — parse, handle rename (deregister old name if name field changed), deregister existing, register new, store hash and mapping.

**`_remove_by_file(filename)`** — pop from both dicts, deregister from registry.

**Thing 3: File watcher**

**`start_watching(poll_interval=2.0)`** — start daemon thread running `_watch_loop`.

**`stop_watching()`** — set stop event, join thread.

**`_watch_loop(poll_interval)`** — loop calling `_check_for_changes()`, catch exceptions, wait on stop event.

**`_check_for_changes()`** — one pass:
- NEW files (not in hashes) → load, call on_change("added", name)
- MODIFIED files (hash differs) → reload, call on_change("updated", name)
- DELETED files (in hashes but not on disk) → remove, call on_change("removed", name)

Uses MD5 hashing, not timestamps.

**Thing 4: Convenience function**

```python
def load_skills_from_directory(skills_dir, registry=None, watch=False,
                                poll_interval=2.0, on_change=None):
    if registry is None: registry = SpecialistRegistry()
    loader = SkillLoader(skills_dir, registry, on_change)
    loader.load_all()
    if watch: loader.start_watching(poll_interval)
    return (registry, loader)
```

### Done criteria

- `parse_skill_yaml` validates required fields, applies defaults, returns SpecialistEntry
- Bad YAML files don't crash the loader
- File watcher detects new/modified/deleted via MD5
- Watcher is daemon thread (auto-dies with main process)
- Agent renaming in YAML handled correctly
- `load_skills_from_directory` works as one-liner

---

## Part 5: Wiring

Create file: `main.py`

```python
from skill_loader import load_skills_from_directory
from orchestrator import TaskEngineOrchestrator
from handoff import StructuredHandoff


async def main():
    # Load skills from YAML with hot-reload
    registry, loader = load_skills_from_directory("./skills", watch=True)

    # Create orchestrator
    engine = TaskEngineOrchestrator(
        name="MainEngine",
        registry=registry,
        router_model="gemini-2.5-flash",
        max_rounds=10,
        token_budget=80_000,
    )

    # Run (standard ADK runner setup)
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=engine, app_name="demo")
    session = await runner.session_service.create_session(app_name="demo", user_id="user1")
    session.state["task"] = "Your task here"

    await runner.run_async(
        session_id=session.id,
        user_id="user1",
        new_message=types.Content(role="user", parts=[types.Part(text="Begin.")])
    )

    # Read the structured handoff
    handoff_data = session.state.get("handoff")
    if handoff_data:
        handoff = StructuredHandoff.model_validate(handoff_data)
        print(f"Verdict: {handoff.verdict}")
        print(f"Status: {handoff.status}")
        print(f"Decisions: {[d.text for d in handoff.decisions]}")
        print(f"Facts: {[f.text for f in handoff.facts]}")
        print(f"Open questions: {[q.text for q in handoff.open_questions]}")

    loader.stop_watching()
```

### For nested societies:

```python
# Inner society
inner_registry, _ = load_skills_from_directory("./skills/research_team")
research_team = SocietyOfMindAgent(
    name="ResearchTeam",
    description="Researches and validates findings.",
    registry=inner_registry,
    max_rounds=5,
)

# Outer orchestrator
outer_registry, loader = load_skills_from_directory("./skills/main", watch=True)
outer_registry.register(SpecialistEntry(
    agent=research_team,
    description=research_team.description,
    capabilities=["research", "validation"],
))
engine = TaskEngineOrchestrator(name="TopLevel", registry=outer_registry, max_rounds=10)
```

---

## File Structure

```
project/
├── handoff.py           # StructuredHandoff Pydantic schema
├── orchestrator.py      # TaskEngineOrchestrator (+ handoff_extractor LlmAgent)
├── society_of_mind.py   # SocietyOfMindAgent (no Summarizer, reads handoff from state)
├── skill_loader.py      # YAML parser + file watcher
├── main.py              # Entry point
└── skills/              # YAML files here
```

---

## End-to-End Flow

```
Round 0: Router → "Researcher"             → runs Researcher
Round 1: Router → "Writer"                 → runs Writer
Round 2: Router → "SecReviewer,PerfReviewer" → runs both parallel
Round 3: Router → "Writer"                 → Writer revises
Round 4: Router → "DONE"                   → loop ends

Post-loop: handoff_extractor runs (LlmAgent with output_schema=StructuredHandoff)
           ADK enforces schema → guaranteed valid Pydantic output
           Stored in session.state["handoff"]
           Zero custom parsing. Zero retry logic. ADK handles it.
```

---

## What Changed From Previous Plan

| Previous plan | This plan |
|---|---|
| Router prompt hacked to output JSON on DONE | Router unchanged — just routes |
| Custom `_parse_handoff` with JSON parsing | ADK `output_schema` handles validation |
| Markdown stripping code | Not needed — ADK enforces format |
| Retry-on-parse-failure logic | Not needed — ADK validates or raises |
| `RoutingDecision.handoff` field added | RoutingDecision unchanged |
| Router does double duty (route + extract) | Router only routes. Extractor is separate. |
| Zero extra LLM calls | One extra LLM call after loop ends |

The tradeoff: one extra LLM call vs removing ALL custom parsing/validation code. ADK guarantees the output is valid. The codebase is simpler and more reliable.
