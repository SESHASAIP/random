# TaskEngine Orchestrator — Society-of-Agents Build Plan

## Read This First

We are building a society-of-agents system where an LLM decides at runtime which specialist agent should work next. Think of it like a team lead who looks at the current state of work and assigns the next person — except the team lead is an LLM.

The system has one job: take a task, run it through a dynamic loop of specialist agents, and produce a final output.

Here is what a run looks like in plain English:

```
You give it: "Write a design doc for fraud detection"

Round 0: System thinks → "We need research first" → runs Researcher agent
Round 1: System thinks → "Research is done, time to write" → runs Writer agent  
Round 2: System thinks → "Draft ready, need security + perf review" → runs both reviewers at the same time
Round 3: System thinks → "Feedback received, Writer needs to fix things" → runs Writer again
Round 4: System thinks → "Revised draft looks good, need final check" → runs FinalReviewer
Round 5: FinalReviewer says "APPROVED" → System detects this keyword → stops → returns the final draft
```

The LLM router is making every "System thinks" decision. No hardcoded order.

---

## How The Pieces Connect

There are 7 pieces. Here is how they relate to each other. Build them in this exact order because each piece depends on the ones before it.

```
Step 1: Data Models
  │      (RoundResult, RoutingDecision, TaskSession)
  │       Define the shape of data that flows through everything else.
  │       Everything below uses these.
  │
Step 2: ConversationContextManager  
  │      Uses: conversation_log (list of RoundResult dicts)
  │      Produces: a trimmed context dict for each specialist agent
  │      Why: Without this, agents see the ENTIRE history and get confused + expensive.
  │
Step 3: ConvergenceDetector
  │      Uses: conversation_log (list of RoundResult dicts)
  │      Produces: a yes/no answer — "should the loop stop?"
  │      Why: Without this, the loop runs forever or stops too early.
  │
Step 4: SpecialistRegistry
  │      Uses: LlmAgent instances + metadata
  │      Produces: a searchable pool of agents with descriptions
  │      Why: The router needs to know what agents exist and what they can do.
  │
Step 5: TaskRouter
  │      Uses: SpecialistRegistry, conversation_log, task description
  │      Produces: a RoutingDecision (who should go next)
  │      Why: This is the brain — the LLM that picks the next agent.
  │
Step 6: AgentExecutor
  │      Uses: a single LlmAgent + InvocationContext
  │      Produces: a RoundResult (structured output with status, error info, etc.)
  │      Why: Wraps every agent call with timeout, retry, and error handling.
  │
Step 7: TaskEngineOrchestrator
         Uses: ALL of the above
         Produces: final output after running the full loop
         This is the main agent — it wires everything together.
```

After Step 7, there is an optional Step 8 (SocietyOfMindAgent) that wraps the orchestrator so it looks like a single agent from the outside.

---

## Step 1: Data Models

### What you are building
Three dataclasses and two enums. These define the shape of every piece of data that moves through the system.

### Why this matters
Without structured data models, agent results are just raw strings. The router cannot tell if an agent succeeded or failed. The convergence detector cannot compare outputs. The session cannot be saved and resumed. Everything downstream depends on these models having the right fields.

### Build instructions

Create file: `orchestrator.py`

**Enum 1 — AgentStatus**

This tracks what happened when we ran a single agent.

```python
class AgentStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
```

When to use each value:
- `SUCCESS`: agent ran, produced non-empty output, no errors
- `ERROR`: agent failed after all retries were exhausted  
- `PARTIAL`: agent produced some output but not everything expected
- `TIMEOUT`: agent exceeded its time limit
- `SKIPPED`: orchestrator decided to skip this agent

**Enum 2 — SessionStatus**

This tracks why the orchestration loop stopped.

```python
class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PAUSED_FOR_HUMAN = "paused_for_human"
    TERMINATED_CONVERGENCE = "terminated_convergence"
    TERMINATED_BUDGET = "terminated_budget"
    TERMINATED_ERROR = "terminated_error"
```

When to use each value:
- `RUNNING`: loop is still going
- `COMPLETED`: router said DONE — task is finished
- `PAUSED_FOR_HUMAN`: loop paused, waiting for human input before continuing
- `TERMINATED_CONVERGENCE`: convergence detector stopped the loop (agent output stopped changing, or explicit approval keyword detected)
- `TERMINATED_BUDGET`: token budget ran out
- `TERMINATED_ERROR`: too many consecutive errors, system cannot recover

**Dataclass 1 — RoundResult**

Every time we run a specialist agent, we get back one of these. This is the structured record of what happened.

```python
@dataclass
class RoundResult:
    agent_name: str            # who ran — example: "Researcher"
    round_number: int          # which loop iteration — example: 0, 1, 2
    output: str                # what the agent produced — the actual text
    status: AgentStatus        # did it work? — SUCCESS, ERROR, etc.
    timestamp: str             # when it ran — ISO format string
    tokens_used: int           # how many tokens it consumed — for budget tracking
    latency_ms: float          # how long it took in milliseconds
    error_message: str | None  # if status is ERROR, what went wrong
    retry_count: int           # how many retries were needed — 0 means first attempt worked
```

Must have a `to_dict()` method that converts the dataclass to a plain dict (because `session.state` needs serializable data).

**Dataclass 2 — RoutingDecision**

Every time the router LLM picks the next agent, we record what it decided and why.

```python
@dataclass
class RoutingDecision:
    round_number: int
    candidates_considered: list[str]  # all agent names that were available
    selected: list[str]               # who was picked — list because parallel selection is possible
    router_reasoning: str             # the LLM's one-line justification
    decision_latency_ms: float        # how long the routing call took
    timestamp: str
    is_parallel: bool                 # True if multiple agents were selected to run at the same time
    is_terminal: bool                 # True if router said DONE
```

Must have a `to_dict()` method.

**Dataclass 3 — TaskSession**

The full state of one orchestration run. This is what gets saved to `session.state` so the run can be paused and resumed.

```python
@dataclass
class TaskSession:
    task: str                          # the original task description
    session_status: SessionStatus      # current state of the run
    conversation_log: list[dict]       # list of RoundResult.to_dict() outputs
    routing_traces: list[dict]         # list of RoutingDecision.to_dict() outputs
    total_tokens_used: int             # running total across all rounds
    total_rounds: int                  # how many rounds have completed
    final_output: str                  # the last successful agent output
```

Must have a `to_dict()` method.

### Done criteria
- All enums and dataclasses are defined
- Every dataclass has a `to_dict()` method that returns a plain dict with enum values converted to strings
- `RoundResult` defaults: `timestamp` auto-generates current UTC time, `tokens_used` defaults to 0, `latency_ms` defaults to 0.0, `error_message` defaults to None, `retry_count` defaults to 0
- `TaskSession` defaults: `session_status` defaults to RUNNING, all lists default to empty, all ints default to 0, `final_output` defaults to empty string

---

## Step 2: ConversationContextManager

### What you are building
A class with one static method that takes the full conversation log and produces a trimmed, relevant context dict for a specialist agent.

### Why this matters
Every specialist agent needs to know what happened before it. But dumping the ENTIRE conversation history into every agent's prompt is bad for two reasons:
1. By round 5, that is 10,000+ tokens of prior output injected into every call — expensive
2. Agents lose focus when overwhelmed with irrelevant history

The context manager solves this by showing each agent only what it needs: full output from the last 2 rounds, compressed summaries for older rounds, and direct-access keys for specific agent outputs.

### Build instructions

**Method signature:**

```python
class ConversationContextManager:

    RECENT_WINDOW = 2  # how many recent rounds get full output

    @staticmethod
    def build_specialist_context(
        conversation_log: list[dict],  # list of RoundResult dicts
        target_agent: str,             # name of the agent we are about to run
        task: str                      # the original task description
    ) -> dict[str, str]:
```

**What it returns:**

A dict with these keys:
- `"task"` → the original task description (always present)
- `"conversation_context"` → the windowed history string (always present)
- `"latest_<agentname>_output"` → direct access to each agent's most recent successful output (optional, one per agent that has produced output)

**How to build the conversation_context string:**

1. If `conversation_log` is empty: return `"No prior conversation. You are the first to work on this task."`

2. Split the log into two groups:
   - `recent` = last 2 entries (controlled by `RECENT_WINDOW`)
   - `older` = everything before the last 2

3. For `recent` entries: include the full output with agent name, round number, and status:
   ```
   [Round 3 — Writer (success)]:
   Here is the full draft text...

   [Round 4 — Reviewer (success)]:
   Two issues remain: first, the latency section...
   ```
   Only include entries with status "success" or "partial". Skip errors — agents should not build on failed output.

4. For `older` entries: compress to one line:
   ```
   Earlier rounds (summary): Researcher(ok) → Writer(ok) → Reviewer(ok)
   ```

5. Combine: older summary first, then recent entries, separated by blank lines.

**How to build the convenience keys:**

Walk the conversation log in REVERSE. For each entry with status "success" or "partial", create a key like `"latest_researcher_output"` (lowercase agent name) with the output text, capped at 2000 characters. Only create each key once (first match wins because we are walking in reverse).

### Done criteria
- Method returns a dict with at least `"task"` and `"conversation_context"` keys
- Recent window is configurable via class constant
- Older rounds are compressed to one-line summaries
- Error-status entries are excluded from the context
- Convenience keys exist for each agent that has produced successful output
- Convenience key values are capped at 2000 characters

---

## Step 3: ConvergenceDetector

### What you are building
A class with one static method that looks at the conversation log and decides if the loop should stop.

### Why this matters
The orchestrator loop needs to stop at the right time. Three things can go wrong:
1. The router says DONE too early because it misjudged the task state
2. The loop runs forever because Agent A keeps giving feedback and Agent B keeps revising but nothing is actually changing
3. The system is broken (errors keep happening) but it keeps trying

The convergence detector catches cases 1, 2, and 3 independently of the router.

### Build instructions

**Method signature:**

```python
class ConvergenceDetector:

    SIMILARITY_THRESHOLD = 0.85
    APPROVED_KEYWORDS = {"APPROVED", "LGTM", "COMPLETE", "DONE", "SHIP IT"}

    @staticmethod
    def check_convergence(
        conversation_log: list[dict]  # list of RoundResult dicts
    ) -> tuple[bool, str]:  # (should_stop, reason_string)
```

**Three checks, run in this order:**

**Check 1 — Explicit approval signal:**
Look at the LAST entry in the conversation log. Check if its output (uppercased) contains any of the `APPROVED_KEYWORDS`. If yes → return `(True, "Explicit approval from <agent_name>")`.

Why this works: We instruct reviewer agents to say "APPROVED" when work is done. This is a concrete, unambiguous signal — more reliable than the router guessing.

**Check 2 — Output similarity (stuck loop detection):**
Group all entries by agent name. For each agent that has produced output 2+ times, compare their last two outputs using `difflib.SequenceMatcher.ratio()`. If the similarity is above `SIMILARITY_THRESHOLD` (0.85) → return `(True, "<agent_name> output converged (similarity: 0.92)")`.

Why this works: If the Writer produces nearly identical output twice in a row, the system is stuck. The reviewer's feedback is not leading to meaningful changes. No point continuing.

**Check 3 — Consecutive errors:**
Look at the last 2 entries. If BOTH have status "error" → return `(True, "Consecutive errors — system cannot recover")`.

Why this works: If two different agents failed in a row, there is likely a systemic issue (model outage, context too large, etc.). Retrying will not help.

**If none of the checks trigger:** return `(False, "")`.

### Done criteria
- Method returns `(bool, str)` tuple
- Check 1 catches any approved keyword in the last entry's output (case-insensitive)
- Check 2 compares last two outputs from the SAME agent using SequenceMatcher
- Check 3 catches two consecutive error-status entries regardless of which agents they are from
- Returns `(False, "")` when conversation log has fewer than 2 entries
- Keywords and similarity threshold are configurable via class constants

---

## Step 4: SpecialistRegistry

### What you are building
Two things: a `SpecialistEntry` dataclass that wraps an agent with metadata, and a `SpecialistRegistry` class that stores and manages them.

### Why this matters
If agent names are hardcoded in the router prompt, adding or removing an agent requires changing the router code. The registry decouples agents from routing — agents register themselves with descriptions and capabilities, and the router queries the registry to build its prompt dynamically.

### Build instructions

**SpecialistEntry — metadata wrapper for one agent:**

```python
@dataclass
class SpecialistEntry:
    agent: LlmAgent              # the ADK agent instance
    description: str             # what this agent does — THE ROUTER READS THIS
    capabilities: list[str]      # tags like ["research", "data_gathering"]
    max_retries: int = 2         # how many retries on failure (used by AgentExecutor)
    timeout_seconds: float = 60.0  # max time per execution (used by AgentExecutor)
    priority: int = 0            # tiebreaker — higher number = preferred
```

The `description` field is critical. This is what the router LLM reads to decide which agent to pick. Write it like you are describing a coworker's job to someone who has never met them.

**SpecialistRegistry — the agent pool:**

```python
class SpecialistRegistry:
    def __init__(self):
        self._entries: dict[str, SpecialistEntry] = {}  # keyed by agent.name

    def register(self, entry: SpecialistEntry) -> None:
        # Add an agent to the pool. Key is entry.agent.name.

    def deregister(self, agent_name: str) -> None:
        # Remove an agent from the pool by name.

    def get(self, agent_name: str) -> SpecialistEntry | None:
        # Look up an agent by name. Returns None if not found.

    def list_agents(self) -> list[str]:
        # Return all registered agent names.

    def build_roster_prompt(self) -> str:
        # Build a formatted string listing all agents for the router prompt.
        # Format per agent:
        # - NAME: Researcher
        #   DESCRIPTION: Gathers facts and background context...
        #   CAPABILITIES: [research, data_gathering]

    def all_agent_instances(self) -> list[LlmAgent]:
        # Return all agent instances (needed for wiring as sub_agents in ADK).
```

### Done criteria
- `register()` stores an entry keyed by agent name
- `deregister()` removes by name without error if name does not exist
- `get()` returns None for unknown names (does not raise)
- `build_roster_prompt()` produces a multi-line string with NAME, DESCRIPTION, CAPABILITIES per agent
- `all_agent_instances()` returns a flat list of LlmAgent objects

---

## Step 5: TaskRouter

### What you are building
A class that makes one LLM call per round to decide which agent(s) should run next. This is the brain of the whole system.

### Why this matters
This replaces AutoGen's GroupChatManager speaker selection. Instead of a black-box selector, we have a controlled, auditable routing call with a specific prompt, low temperature, and structured output.

### Build instructions

**Constructor:**

```python
class TaskRouter:
    def __init__(self, model: str, registry: SpecialistRegistry):
        self.model = model
        self.registry = registry
```

**The router system prompt (store as class constant):**

```
You are a task routing engine for a society-of-agents system.
Your job: decide which specialist agent(s) should handle the NEXT step.

RULES:
1. Pick agents based on DESCRIPTION and CAPABILITIES, not just names.
2. If one agent can handle the next step alone, return just that name.
3. If multiple agents can work INDEPENDENTLY in parallel, return comma-separated names.
   ONLY do this when the tasks are truly independent (e.g., security review + style review).
4. If the task is COMPLETE (approved, no more work needed), return: DONE
5. If a previous agent errored, decide: retry the same agent OR skip to another.
6. Don't loop the same agent more than 2 times in a row unless correcting an error.
7. Consider the STATUS of previous rounds — don't build on errored output.

RESPOND WITH ONLY: agent_name OR agent1,agent2 OR DONE
Then on a NEW LINE, write a one-sentence justification.
```

**The user prompt (built per round):**

Combine three things:
1. `TASK: <the original task>`
2. `AVAILABLE AGENTS:` followed by the output of `registry.build_roster_prompt()`
3. `CONVERSATION HISTORY:` followed by the last 6 rounds from the conversation log, each formatted as:
   ```
   [Round 2 | Writer | status=success]: First 300 chars of output...
   ```
   Only show last 6 rounds to keep the router prompt short.
4. `TOKEN BUDGET REMAINING: ~<number> tokens` — so the router knows to wrap up if budget is low.

**The route method:**

```python
async def route(
    self,
    task: str,
    conversation_log: list[dict],
    token_budget_remaining: int
) -> RoutingDecision:
```

Steps:
1. Build the prompt from task + registry roster + last 6 log entries + budget
2. Call the LLM with `temperature=0.1` and `max_output_tokens=100` (routing responses are short, low temp for consistency)
3. Parse the response: split on first newline — line 1 is the selection, line 2 is the reasoning
4. If line 1 is "DONE" → return a RoutingDecision with `is_terminal=True` and empty `selected` list
5. If line 1 contains commas → split on commas, match each name against registered agents → `is_parallel=True`
6. If line 1 is a single name → match against registered agents → `is_parallel=False`
7. Matching: try exact match first. If no exact match, try case-insensitive substring match (because the LLM might say "the Researcher" instead of "Researcher")
8. If nothing matches → return a RoutingDecision with `is_terminal=True` (fail safe — stop rather than crash)

**LLM call configuration:**
- `temperature=0.1` — low randomness, we want consistent routing
- `max_output_tokens=100` — routing responses are 2-3 lines max
- Use `system_instruction` for the router system prompt, `contents` for the per-round user prompt

### Done criteria
- `route()` returns a `RoutingDecision` every time (never raises)
- Single agent selection produces `is_parallel=False`
- Comma-separated selection produces `is_parallel=True` with multiple names in `selected`
- "DONE" response produces `is_terminal=True`
- Unparseable response produces `is_terminal=True` (fail safe)
- Fuzzy matching works for minor LLM output variations
- Router only sees last 6 rounds of history (not full log)
- Decision latency is measured and included in the RoutingDecision

---

## Step 6: AgentExecutor

### What you are building
A class with one static method that runs a single specialist agent with timeout protection, retry logic, and structured error capture.

### Why this matters
If we call `agent.run_async(ctx)` directly and the agent times out or throws an exception, the entire orchestrator crashes. In production, this means the task is lost and unrecoverable. The executor wraps every agent call so failures are captured as data (a RoundResult with status=ERROR) instead of exceptions that kill the process.

### Build instructions

**Method signature:**

```python
class AgentExecutor:

    @staticmethod
    async def execute(
        agent: LlmAgent,
        ctx: InvocationContext,
        round_number: int,
        max_retries: int = 2,
        timeout_seconds: float = 60.0
    ) -> tuple[RoundResult, list]:  # (structured result, list of yielded events)
```

**Logic (pseudocode):**

```
for attempt in range(max_retries + 1):
    try:
        start timer
        run agent.run_async(ctx) wrapped in asyncio.wait_for(timeout=timeout_seconds)
        collect all events from the async generator
        extract text output from events
        stop timer

        if output is empty or whitespace-only:
            raise ValueError("Agent returned empty output")

        return RoundResult(status=SUCCESS, output=text, latency=elapsed, retry_count=attempt)

    except asyncio.TimeoutError:
        log warning: "Agent <name> timed out (attempt X/Y)"
        last_error = "Timeout after Xs"

    except any Exception as e:
        log warning: "Agent <name> failed (attempt X/Y): <error>"
        last_error = str(e)

    if more retries remaining:
        wait 2^attempt seconds (exponential backoff: 1s, 2s, 4s)

# all retries exhausted
return RoundResult(status=ERROR, output="AGENT_ERROR: <last_error>", error_message=last_error, retry_count=max_retries)
```

**Important details:**
- Use `asyncio.wait_for()` for timeout enforcement — it cancels the coroutine if time expires
- The events list collects all events yielded by the agent (these get yielded upstream by the orchestrator)
- Empty output is treated as an error and triggers retry — agents sometimes return empty responses on transient model issues
- Exponential backoff: `await asyncio.sleep(2 ** attempt)` — waits 1s, then 2s, then 4s
- The method NEVER raises an exception — it always returns a RoundResult. This is critical because the orchestrator loop must continue even when agents fail.

**Helper method for asyncio.wait_for compatibility:**

`agent.run_async(ctx)` returns an async generator, but `asyncio.wait_for` expects a coroutine. You need a helper that collects events from the generator and yields them:

```python
@staticmethod
async def _collect_events(agent, ctx):
    async for event in agent.run_async(ctx):
        yield event
```

### Done criteria
- Method never raises exceptions — always returns a RoundResult
- Timeout is enforced via `asyncio.wait_for`
- Retries use exponential backoff (2^attempt seconds)
- Empty output triggers retry
- All events from the agent are collected and returned alongside the RoundResult
- Error messages include the actual exception text

---

## Step 7: TaskEngineOrchestrator

### What you are building
The main agent. It extends ADK's `BaseAgent` and wires together all the components from Steps 1-6 into a single orchestration loop. This is the agent you instantiate and run.

### Why this matters
This is the entire system assembled into one runnable agent. It replaces AutoGen's GroupChat + GroupChatManager with a custom ADK agent that has full control over routing, execution, context management, and termination.

### Build instructions

**Constructor:**

```python
class TaskEngineOrchestrator(BaseAgent):
    def __init__(
        self,
        name: str,
        registry: SpecialistRegistry,
        router_model: str = "gemini-2.5-flash",
        max_rounds: int = 10,
        token_budget: int = 100_000,
        human_review_after: int | None = None,
    ):
```

In the constructor:
1. Store all parameters
2. Create a `TaskRouter` instance with the model and registry
3. Create a `ConversationContextManager` instance
4. Create a `ConvergenceDetector` instance
5. Call `super().__init__(name=name, sub_agents=registry.all_agent_instances())` — this registers all specialist agents in the ADK hierarchy

**The main loop — `_run_async_impl` method:**

This is the core of the system. It is an async generator method required by ADK's `BaseAgent`.

```python
async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator:
```

Here is the loop logic, step by step:

```
1. LOAD OR CREATE SESSION
   - Check if session.state["task_session"] exists (resuming from pause)
   - If yes: load it into a TaskSession object
   - If no: create a new TaskSession with task from session.state["task"]
   
   If resuming from PAUSED_FOR_HUMAN status:
   - Read session.state["human_review_input"]
   - Append it to conversation_log as a HUMAN_REVIEWER entry
   - Set status back to RUNNING

2. MAIN LOOP (while status is RUNNING and rounds < max_rounds and tokens < budget):

   2a. CHECK HUMAN PAUSE
       - If human_review_after is set AND round_number > 0 AND round_number is divisible by human_review_after:
         → Set status to PAUSED_FOR_HUMAN
         → Save session to state
         → Break out of loop

   2b. CHECK CONVERGENCE
       - Call ConvergenceDetector.check_convergence(conversation_log)
       - If converged: set status to TERMINATED_CONVERGENCE, break

   2c. CHECK TOKEN BUDGET
       - If remaining budget < 1000: set status to TERMINATED_BUDGET, break

   2d. ROUTE
       - Call TaskRouter.route(task, conversation_log, remaining_budget)
       - Append the RoutingDecision to routing_traces
       - If routing decision is terminal: set status to COMPLETED, break

   2e. EXECUTE AGENT(S)
       - If routing decision selected multiple agents (is_parallel=True):
           → Call _execute_parallel() — see below
       - If routing decision selected one agent:
           → Call _execute_single() — see below

   2f. YIELD EVENTS
       - For each event returned by the executor: yield it upstream
       - This is what makes the orchestrator's output visible to the caller

   2g. UPDATE SESSION
       - Append each RoundResult dict to conversation_log
       - Add tokens_used to total_tokens_used
       - Increment total_rounds

3. AFTER LOOP ENDS
   - Find the last successful output in conversation_log (walk backwards, find first entry with status success or partial)
   - Set it as final_output
   - Save session to session.state["task_session"]
   - Also save final_output and session_status to session.state for easy access
```

**The `_execute_single` method:**

```python
async def _execute_single(self, agent_name, ctx, round_num, session) -> tuple[list[RoundResult], list]:
```

Steps:
1. Look up the agent in the registry: `entry = registry.get(agent_name)`
2. If not found: return a RoundResult with status=ERROR and message "not found in registry"
3. Build context: call `ConversationContextManager.build_specialist_context()` with the conversation log, agent name, and task
4. Inject context into session state: for each key-value in the context dict, set `ctx.session.state[key] = value`
5. Run the agent: call `AgentExecutor.execute()` with the agent, ctx, round_number, and the entry's max_retries and timeout_seconds
6. Return the RoundResult and events

**The `_execute_parallel` method:**

```python
async def _execute_parallel(self, agent_names, ctx, round_num, session) -> tuple[list[RoundResult], list]:
```

Steps:
1. Build context ONCE (shared across all parallel agents)
2. Inject context into session state
3. Create a list of `AgentExecutor.execute()` coroutine calls, one per agent
4. Run all of them with `asyncio.gather(*tasks, return_exceptions=True)`
5. For each result: if it is an Exception, create an error RoundResult. Otherwise, unpack the (RoundResult, events) tuple.
6. Return all RoundResults and all events

Important: each parallel agent MUST write to a different `output_key` in session state. If two agents write to the same key, one overwrites the other. This is configured at agent definition time, not here.

**Session load/save helpers:**

```python
def _load_or_create_session(self, ctx) -> TaskSession:
    existing = ctx.session.state.get("task_session")
    if existing and isinstance(existing, dict):
        # Reconstruct TaskSession from dict
        return TaskSession(
            task=existing["task"],
            session_status=SessionStatus(existing["session_status"]),
            conversation_log=existing.get("conversation_log", []),
            # ... all fields
        )
    return TaskSession(task=ctx.session.state.get("task", ""))

def _save_session(self, ctx, session):
    ctx.session.state["task_session"] = session.to_dict()
    ctx.session.state["final_output"] = session.final_output
    ctx.session.state["session_status"] = session.session_status.value
```

### Done criteria
- Extends `BaseAgent` with all specialist agents registered as `sub_agents`
- Main loop runs until one of five stop conditions: COMPLETED, PAUSED_FOR_HUMAN, TERMINATED_CONVERGENCE, TERMINATED_BUDGET, or max_rounds hit
- Each round: check pause → check convergence → check budget → route → execute → update session
- Parallel execution uses `asyncio.gather` and handles individual agent failures without crashing the batch
- Context is injected into session.state before each agent execution
- Session is saved to session.state on every exit path (normal completion, pause, error, budget)
- Events from agent execution are yielded upstream

---

## Step 8 (Optional): SocietyOfMindAgent

### What you are building
A wrapper that hides a full TaskEngineOrchestrator behind a single agent interface. The internal debate is invisible to the outside — only a summarized final answer is emitted.

### Why this matters
Sometimes you want a team of agents to debate internally but present one clean answer. This wrapper makes it possible to use a full orchestrator as a sub-agent inside a bigger system. The outside world sees one agent, one output.

### Build instructions

**Create file:** `society_of_mind.py`

**Constructor:**

```python
class SocietyOfMindAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        registry: SpecialistRegistry,
        router_model: str = "gemini-2.5-flash",
        summarizer_model: str = "gemini-2.5-flash",
        max_rounds: int = 6,
        token_budget: int = 50_000,
        description: str = "",
    ):
```

Inside the constructor:
1. Create a `TaskEngineOrchestrator` as `self.inner_engine` (this runs the debate)
2. Create a `LlmAgent` as `self.summarizer` (this distills the debate into one answer)
3. Call `super().__init__(name=name, description=description, sub_agents=[self.inner_engine, self.summarizer])`

The summarizer's instruction should tell it to:
- Read the conversation context (injected from the debate)
- Produce ONE clean, polished output
- NOT mention other agents, rounds, or the debate process
- Write as if it is the sole author

**The main method — `_run_async_impl`:**

```python
async def _run_async_impl(self, ctx):

    # Step 1: Run the full internal debate
    # IMPORTANT: do NOT yield these events — they are the internal monologue
    async for event in self.inner_engine.run_async(ctx):
        pass  # swallow everything

    # Step 2: Build context for the summarizer from the debate results
    task_session = ctx.session.state.get("task_session", {})
    summary_context = ConversationContextManager.build_specialist_context(
        conversation_log=task_session.get("conversation_log", []),
        target_agent=self.summarizer.name,
        task=task_session.get("task", "")
    )
    for key, value in summary_context.items():
        ctx.session.state[key] = value

    # Step 3: Run the summarizer
    # IMPORTANT: DO yield these events — this is the only output the outside sees
    async for event in self.summarizer.run_async(ctx):
        yield event
```

### Nesting example

A SocietyOfMindAgent can be registered as a specialist in another orchestrator:

```
Top-level TaskEngine
├── ResearchTeam (SocietyOfMind — internally: Researcher + Analyst + Critic)
├── WritingTeam (SocietyOfMind — internally: Drafter + Editor)
└── FinalReviewer (single LlmAgent)
```

The top-level router sees "ResearchTeam" as one agent with a description. It has no idea there are three agents debating inside.

### Done criteria
- Inner engine events are NOT yielded (swallowed)
- Only summarizer events are yielded
- The summarizer receives the debate context via ConversationContextManager
- The SocietyOfMindAgent can be registered in a SpecialistRegistry and used as a sub-agent in another orchestrator

---

## Wiring Example

Here is how to wire everything together for a concrete use case. This is a code review pipeline with 5 specialists.

### Define the specialists

Each specialist is a standard `LlmAgent`. Key things to get right:
- `description`: written for the router LLM — describe what this agent does and when to use it
- `instruction`: includes `{task}` and `{conversation_context}` — these are injected by the context manager
- `output_key`: unique per agent — this is where the agent writes its output in session state

```python
researcher = LlmAgent(
    name="Researcher",
    model="gemini-2.5-flash",
    description="Gathers facts and background context. Good first step when the team needs foundational information.",
    instruction="""You are a researcher.
    TASK: {task}
    CONTEXT: {conversation_context}
    Provide factual findings.""",
    output_key="researcher_output"
)

writer = LlmAgent(
    name="Writer",
    model="gemini-2.5-flash",
    description="Writes and revises content based on research and feedback. Call after research is available.",
    instruction="""You are a writer.
    TASK: {task}
    CONTEXT: {conversation_context}
    Write or revise content based on available research and feedback.""",
    output_key="writer_output"
)

# ... define security_reviewer, performance_reviewer, final_reviewer similarly
# Make sure each has a unique output_key
```

### Build the registry

```python
registry = SpecialistRegistry()

registry.register(SpecialistEntry(
    agent=researcher,
    description=researcher.description,
    capabilities=["research", "data_gathering"],
    max_retries=2,
    timeout_seconds=45.0
))

# Repeat for each specialist
```

### Create and run the orchestrator

```python
engine = TaskEngineOrchestrator(
    name="CodeReviewEngine",
    registry=registry,
    router_model="gemini-2.5-flash",
    max_rounds=10,
    token_budget=80_000
)

# Set up ADK runner
runner = InMemoryRunner(agent=engine, app_name="my_app")
session = await runner.session_service.create_session(app_name="my_app", user_id="user1")

# Set the task
session.state["task"] = "Write a technical design doc for fraud detection"

# Run
response = await runner.run_async(
    session_id=session.id,
    user_id="user1",
    new_message=Content(role="user", parts=[Part(text="Begin.")])
)

# Get results
result = session.state["task_session"]
print(result["final_output"])
print(result["session_status"])  # "completed", "terminated_convergence", etc.
```

---

## Error Handling Quick Reference

| What goes wrong | What happens | What you see |
|---|---|---|
| Agent times out | AgentExecutor retries with backoff (1s, 2s, 4s). If all retries fail, returns RoundResult with status=TIMEOUT | Router sees the timeout in conversation log and routes accordingly |
| Agent returns empty output | Treated as error, triggers retry | Same as timeout |
| Agent throws exception | Caught, retried with backoff | RoundResult with status=ERROR and error_message |
| Router LLM returns gibberish | Fuzzy name matching attempted. If no match, treated as terminal | Orchestrator stops gracefully with whatever output exists |
| Two consecutive errors from any agents | ConvergenceDetector stops the loop | Session status = TERMINATED_ERROR |
| Token budget runs out | Orchestrator stops at next loop iteration | Session status = TERMINATED_BUDGET |
| Same agent produces identical output twice | ConvergenceDetector detects >85% similarity, stops loop | Session status = TERMINATED_CONVERGENCE |

---

## Known Limitations To Keep In Mind

1. **Parallel agents must have unique output_keys.** If two agents running in parallel write to the same session state key, one overwrites the other. Always give each parallel agent its own output_key.

2. **Router adds one LLM call per round.** For simple workflows where the order is always research → write → review, a hardcoded SequentialAgent is cheaper. Use this orchestrator when the order genuinely needs to vary at runtime.

3. **SocietyOfMind hides all progress.** The inner debate produces no visible output until the summarizer finishes. If the debate takes 60 seconds, the user sees nothing during that time.

4. **Long agent outputs can still pressure context windows.** The context manager mitigates this but does not eliminate it. If agents routinely produce 5000+ token outputs, even the 2-round window is large. Add output length guidance in specialist instructions.

5. **Single session scope.** The orchestrator operates within one ADK session. Chaining tasks across sessions requires external coordination.
