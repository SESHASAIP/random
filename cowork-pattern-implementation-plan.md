# Co-Work Pattern — Implementation Plan

## Architecture Summary

An optimistic orchestrator that takes a markdown spec, decomposes it via a persona-driven parse, plans subtasks with skill assignment, wires dependencies deterministically, and executes in parallel waves.

```
spec.md → Persona Parse (LLM) → Plan (LLM) → Wire (code) → Execute (wave-parallel) → Assemble → Output
```

---

## Phase 1: Persona Detection + Persona Parse (Step 1)

**Goal**: Detect what the input needs, route to the right persona, and parse through that persona's lens. A persona is NOT a job title — it's a **skill cluster with domain understanding**. The input decides which persona activates. The persona's skills are the ONLY skills the planner will see, eliminating noise and wrong matches.

### Core Concept

```
Input: "Build a customer dashboard with API and database"

Available Personas:
  full-stack-builder:  [frontend-design, api-builder, database-query]
  data-analyst:        [database-query, data-viz, xlsx-builder]
  content-creator:     [docx-writer, pdf-builder, markdown-gen]

Detection → full-stack-builder activated
Planner sees ONLY: [frontend-design, api-builder, database-query]
```

The planner never sees data-viz, docx-writer, or any irrelevant skill. Zero noise = best accuracy.

### Persona Definition

Each persona wraps a set of skills and carries domain understanding of how those skills work together.

```python
from dataclasses import dataclass, field

@dataclass
class Persona:
    name: str
    description: str                    # what this persona handles
    trigger_keywords: list[str]         # words that activate this persona
    skills: list[str]                   # skill names this persona owns
    domain_context: str                 # how this persona thinks about problems
    skill_instructions: dict = field(default_factory=dict)  # skill_name → full SKILL.md (loaded at execution)

# Example personas
PERSONAS = [
    Persona(
        name="full-stack-builder",
        description="Builds complete applications with frontend, API, and database layers",
        trigger_keywords=["dashboard", "app", "api", "frontend", "backend", "crud",
                          "endpoint", "database", "table", "component", "ui", "rest"],
        skills=["frontend-design", "api-builder", "database-query"],
        domain_context="""You understand full-stack architecture. You think in layers:
        data layer (what gets stored), service layer (what logic runs),
        presentation layer (what the user sees). You know data flows
        upward: database → API → frontend. You sequence accordingly."""
    ),
    Persona(
        name="data-analyst",
        description="Analyzes data, builds visualizations, creates reports from datasets",
        trigger_keywords=["analyze", "chart", "graph", "csv", "dataset", "metrics",
                          "report", "visualization", "statistics", "trend", "aggregate"],
        skills=["database-query", "data-viz", "xlsx-builder"],
        domain_context="""You understand data pipelines. You think in:
        extraction (get the data), transformation (clean and shape),
        presentation (visualize and report). You prioritize data
        integrity and clear communication of findings."""
    ),
    Persona(
        name="content-creator",
        description="Creates documents, presentations, written content",
        trigger_keywords=["document", "report", "presentation", "slides", "write",
                          "draft", "memo", "article", "blog", "pdf", "docx", "pptx"],
        skills=["docx-writer", "pdf-builder", "markdown-gen", "pptx-builder"],
        domain_context="""You understand content structure. You think in:
        audience (who reads this), structure (how it flows),
        format (what medium serves it best). You prioritize
        clarity, hierarchy, and professional formatting."""
    ),
]
```

### Persona Detection

Deterministic keyword matching against the input spec. No LLM call needed — fast and predictable.

```python
def detect_persona(raw_md: str, personas: list[Persona]) -> Persona:
    """Score each persona by keyword hits against the input. Highest score wins."""
    raw_lower = raw_md.lower()
    scores = {}

    for persona in personas:
        score = 0
        for keyword in persona.trigger_keywords:
            # Count occurrences, not just presence — repeated terms signal stronger fit
            score += raw_lower.count(keyword.lower())
        scores[persona.name] = score

    best = max(scores, key=scores.get)

    if scores[best] == 0:
        # No keywords matched — fall back to a general-purpose persona
        # or return persona with broadest skill set
        return _get_default_persona(personas)

    return next(p for p in personas if p.name == best)


def _get_default_persona(personas: list[Persona]) -> Persona:
    """Fallback: persona with the most skills = broadest coverage."""
    return max(personas, key=lambda p: len(p.skills))
```

### Persona Parse Prompt

The persona's `domain_context` shapes HOW the LLM interprets the spec. The persona's `skills` are the ONLY skills visible. This is the key insight — the LLM can't assign wrong skills because wrong skills don't exist in its context.

```python
PERSONA_PARSE_PROMPT = """You are a {persona_name}.

{domain_context}

Your job is NOT to create tasks. Your job is to UNDERSTAND the spec deeply and output a structured interpretation that makes the planner's job trivial.

## INPUT SPEC
{raw_md}

## YOUR SKILLS (these are the ONLY skills available — do not reference any others)
{skills_formatted}

## EXTRACT THE FOLLOWING

1. goal: Restate the goal in one clear sentence. Resolve any ambiguity.

2. components: What are the distinct system pieces?
   For each:
   - name
   - responsibility (one sentence)
   - data_owns: what data it manages
   - exposes: what it provides to other components
   - depends_on: what it needs from other components
   - best_skill: which of YOUR skills fits (must be from the list above)
   - complexity: S/M/L (S = <50 lines, M = <200 lines, L = needs splitting)

3. data_flow: How does data move through the system?
   List edges as: source_component.output_type → target_component

4. requirements_enriched: For each original requirement:
   - original_text
   - implied_output: what artifact does this produce
   - implied_format: file type (sql, py, jsx, json, etc)
   - acceptance: one testable statement of "done"
   - belongs_to_component: which component from above

5. risks: What's ambiguous, underspecified, or likely to cause problems? Max 3.

6. constraints: Global rules (language, framework, infra) stated or implied.

## OUTPUT FORMAT (JSON, no markdown fences)
{{
  "goal": "...",
  "components": [...],
  "data_flow": [...],
  "requirements_enriched": [...],
  "risks": [...],
  "constraints": [...]
}}"""
```

### Data Models

```python
@dataclass
class Component:
    name: str
    responsibility: str
    data_owns: list[str]
    exposes: list[str]
    depends_on: list[str]
    best_skill: str
    complexity: str  # S, M, L

@dataclass
class EnrichedRequirement:
    original_text: str
    implied_output: str
    implied_format: str
    acceptance: str
    belongs_to_component: str

@dataclass
class EnrichedSpec:
    goal: str
    components: list[Component]
    data_flow: list[str]
    requirements_enriched: list[EnrichedRequirement]
    risks: list[str]
    constraints: list[str]
    persona: str                  # which persona was activated
    available_skills: list[str]   # only the persona's skills — planner sees nothing else
```

### Execution

```python
def format_skills_for_prompt(persona: Persona, skill_configs: dict) -> str:
    """Format only this persona's skills for the prompt."""
    lines = []
    for skill_name in persona.skills:
        if skill_name in skill_configs:
            cfg = skill_configs[skill_name]
            lines.append(f"- {skill_name}: {cfg.get('summary', skill_name)}")
    return "\n".join(lines)


async def persona_parse(raw_md: str, personas: list[Persona], skill_configs: dict, llm) -> EnrichedSpec:
    # 1. Detect persona (deterministic — no LLM)
    persona = detect_persona(raw_md, personas)

    # 2. Format only this persona's skills
    skills_formatted = format_skills_for_prompt(persona, skill_configs)

    # 3. Single LLM call with persona-scoped context
    prompt = PERSONA_PARSE_PROMPT.format(
        persona_name=persona.name,
        domain_context=persona.domain_context,
        raw_md=raw_md,
        skills_formatted=skills_formatted,
    )

    raw = await llm.call(prompt, model="strong")
    data = json.loads(raw)

    return EnrichedSpec(
        goal=data["goal"],
        components=[Component(**c) for c in data["components"]],
        data_flow=data["data_flow"],
        requirements_enriched=[EnrichedRequirement(**r) for r in data["requirements_enriched"]],
        risks=data["risks"],
        constraints=data["constraints"],
        persona=persona.name,
        available_skills=persona.skills,
    )
```

### Why This Eliminates Wrong Hits

```
WITHOUT persona scoping (old approach):
  Planner sees 15 skills → assigns "docx-writer" to build a React component
  because the description mentioned "write" → WRONG HIT

WITH persona scoping (this approach):
  Input detected as full-stack → persona has [frontend, api, db] only
  Planner literally cannot assign docx-writer → it doesn't exist in context
  → ZERO wrong hits for out-of-domain skills
```

**Deliverables**: `Persona` dataclass, `PERSONAS` config, `detect_persona()`, `PERSONA_PARSE_PROMPT`, `EnrichedSpec` dataclass, `persona_parse()` function.

---

## Phase 2: Planner (Step 2)

**Goal**: A single LLM call that converts the enriched spec into executable subtasks with skill assignments and output contracts. This is mostly template-filling since the persona parse did the hard thinking.

### Prompt

```python
PLANNER_PROMPT = """You are a task planner. Convert this enriched spec into executable subtasks.

## ENRICHED SPEC
{enriched_spec_json}

## RULES
- One subtask per requirement (split further if complexity is L)
- Use the skill already suggested in best_skill (override only if clearly wrong)
- Each subtask must be completable by a single agent in one session
- Use implied_output and implied_format from requirements as your output contract
- Use acceptance from requirements as your acceptance criteria

## OUTPUT FORMAT (JSON, no markdown fences)
{
  "tasks": [
    {
      "task_id": "t1",
      "component": "component name",
      "description": "specific action, one sentence",
      "assigned_skill": "skill-name",
      "size": "S|M",
      "consumes": ["input_type_1"],
      "produces": ["output_type_1"],
      "output_contract": {
        "format": "file extension",
        "must_contain": ["string markers to verify"]
      },
      "acceptance": "one testable statement"
    }
  ]
}"""
```

### Data Models

```python
@dataclass
class OutputContract:
    format: str                     # file extension
    must_contain: list[str]         # markers for validation

@dataclass
class SubTask:
    task_id: str
    component: str
    description: str
    assigned_skill: str
    size: str
    consumes: list[str]
    produces: list[str]
    output_contract: OutputContract
    acceptance: str
    dependencies: list[str] = field(default_factory=list)  # filled in wire step
    wave: int = -1                                          # filled in wire step
```

### Execution

```python
async def plan(enriched_spec: EnrichedSpec, llm) -> list[SubTask]:
    prompt = PLANNER_PROMPT.format(
        enriched_spec_json=json.dumps(enriched_spec.__dict__, default=str)
    )

    raw = await llm.call(prompt, model="fast")  # fast model, template-filling
    data = json.loads(raw)

    tasks = []
    for t in data["tasks"]:
        contract = OutputContract(**t.pop("output_contract"))
        tasks.append(SubTask(**t, output_contract=contract))

    return tasks
```

**Deliverables**: `PLANNER_PROMPT`, `SubTask` / `OutputContract` dataclasses, `plan()` function.

---

## Phase 3: Wire Dependencies (Step 3)

**Goal**: Deterministic code that builds a DAG from consumes/produces, detects cycles, and computes execution waves. Zero LLM calls.

```python
from collections import defaultdict, deque

def wire_dependencies(tasks: list[SubTask]) -> list[SubTask]:
    """Wire task dependencies based on consumes/produces matching."""

    # 1. Build producer index: output_type → task_id
    producer_map: dict[str, str] = {}
    for task in tasks:
        for output in task.produces:
            producer_map[output] = task.task_id

    # 2. Wire dependencies
    for task in tasks:
        task.dependencies = []
        for inp in task.consumes:
            if inp in producer_map:
                dep_id = producer_map[inp]
                if dep_id != task.task_id:
                    task.dependencies.append(dep_id)

    # 3. Detect cycles
    if _has_cycle(tasks):
        raise ValueError("Circular dependency detected in task DAG")

    # 4. Compute waves (topological layers)
    task_map = {t.task_id: t for t in tasks}
    in_degree = {t.task_id: len(t.dependencies) for t in tasks}
    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    wave = 0

    while queue:
        next_queue = deque()
        for _ in range(len(queue)):
            tid = queue.popleft()
            task_map[tid].wave = wave
            for t in tasks:
                if tid in t.dependencies:
                    in_degree[t.task_id] -= 1
                    if in_degree[t.task_id] == 0:
                        next_queue.append(t.task_id)
        queue = next_queue
        wave += 1

    return tasks


def _has_cycle(tasks: list[SubTask]) -> bool:
    """Kahn's algorithm — if not all nodes visited, there's a cycle."""
    task_ids = {t.task_id for t in tasks}
    in_degree = {t.task_id: len(t.dependencies) for t in tasks}
    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    visited = 0

    while queue:
        tid = queue.popleft()
        visited += 1
        for t in tasks:
            if tid in t.dependencies:
                in_degree[t.task_id] -= 1
                if in_degree[t.task_id] == 0:
                    queue.append(t.task_id)

    return visited != len(task_ids)


def group_by_wave(tasks: list[SubTask]) -> dict[int, list[SubTask]]:
    """Group tasks into execution waves."""
    waves = defaultdict(list)
    for task in tasks:
        waves[task.wave].append(task)
    return dict(sorted(waves.items()))
```

**Deliverables**: `wire_dependencies()`, `_has_cycle()`, `group_by_wave()`.

---

## Phase 4: Execute (Step 4)

**Goal**: Walk the DAG wave by wave. Each wave runs its tasks in parallel. Each task gets a scoped agent with only its assigned skill's full instructions.

### Subtask Executor

```python
from pathlib import Path

async def execute_subtask(
    task: SubTask,
    completed: dict[str, any],
    skill_registry: dict[str, SkillFingerprint],
    skill_md_paths: dict[str, str],  # skill_name → path to SKILL.md
    llm,
) -> dict:
    """Execute a single subtask with a scoped agent."""

    # 1. Load full SKILL.md (only now, not at plan time)
    skill_instructions = Path(skill_md_paths[task.assigned_skill]).read_text()

    # 2. Gather outputs from dependency tasks
    dep_context = ""
    for dep_id in task.dependencies:
        if dep_id in completed:
            dep_context += f"\n--- Output from {dep_id} ---\n{completed[dep_id]['output']}\n"

    # 3. Build scoped system prompt
    system_prompt = f"""{skill_instructions}

## YOUR TASK
{task.description}

## INPUT CONTEXT
{dep_context if dep_context else "No prior task outputs needed."}

## ACCEPTANCE CRITERIA
{task.acceptance}

Execute this task completely. Output your result."""

    # 4. Run agent (with only the skill's tools)
    result = await llm.call(system_prompt, model="strong")

    return {
        "task_id": task.task_id,
        "output": result,
        "assigned_skill": task.assigned_skill,
    }
```

### Contract Validation

```python
def validate_contract(result: dict, contract: OutputContract) -> tuple[bool, str]:
    """Deterministic check — does the output match the contract?"""
    output = result.get("output", "")

    for marker in contract.must_contain:
        if marker not in output:
            return False, f"Missing required content: {marker}"

    return True, "passed"
```

### Wave Executor

```python
import asyncio

MAX_RETRIES = 2

async def execute_dag(
    tasks: list[SubTask],
    skill_registry: dict[str, SkillFingerprint],
    skill_md_paths: dict[str, str],
    llm,
) -> dict[str, any]:
    """Execute all tasks wave by wave, parallel within each wave."""
    waves = group_by_wave(tasks)
    completed = {}

    for wave_num in sorted(waves.keys()):
        wave_tasks = waves[wave_num]

        results = await asyncio.gather(*[
            execute_subtask(task, completed, skill_registry, skill_md_paths, llm)
            for task in wave_tasks
        ])

        for task, result in zip(wave_tasks, results):
            passed, reason = validate_contract(result, task.output_contract)

            if passed:
                completed[task.task_id] = result
            else:
                # Replan and retry
                revised_result = await replan_and_retry(
                    task, reason, completed, skill_registry, skill_md_paths, llm
                )
                completed[task.task_id] = revised_result

    return completed
```

### Replan (on failure only)

```python
REPLAN_PROMPT = """This task failed validation.

TASK: {task_description}
ASSIGNED SKILL: {assigned_skill}
FAILURE REASON: {reason}
PREVIOUS OUTPUT (first 500 chars): {prev_output}

Produce a corrected output that satisfies:
- Acceptance criteria: {acceptance}
- Must contain: {must_contain}"""


async def replan_and_retry(
    task: SubTask,
    reason: str,
    completed: dict,
    registry: dict,
    skill_md_paths: dict,
    llm,
    max_retries: int = MAX_RETRIES,
) -> dict:
    """Retry a failed task with error context. Max retries, then return best effort."""
    last_result = None

    for attempt in range(max_retries):
        prompt = REPLAN_PROMPT.format(
            task_description=task.description,
            assigned_skill=task.assigned_skill,
            reason=reason,
            prev_output=str(last_result)[:500] if last_result else "N/A",
            acceptance=task.acceptance,
            must_contain=task.output_contract.must_contain,
        )

        result = await execute_subtask(task, completed, registry, skill_md_paths, llm)
        passed, reason = validate_contract(result, task.output_contract)

        if passed:
            return result
        last_result = result

    # Max retries exhausted — return last result with warning
    last_result["warning"] = f"Failed validation after {max_retries} retries: {reason}"
    return last_result
```

**Deliverables**: `execute_subtask()`, `validate_contract()`, `execute_dag()`, `replan_and_retry()`.

---

## Phase 5: Assemble (Step 5)

**Goal**: Combine all task outputs into a final deliverable. Deterministic for simple cases, optional LLM call for integration summary.

```python
async def assemble(
    completed: dict[str, any],
    enriched_spec: EnrichedSpec,
    llm=None,
) -> dict:
    """Combine all task outputs into final deliverable."""

    # Collect all outputs
    all_outputs = {}
    warnings = []

    for task_id, result in completed.items():
        all_outputs[task_id] = result["output"]
        if "warning" in result:
            warnings.append(f"{task_id}: {result['warning']}")

    # Check all acceptance criteria from enriched spec
    unmet = []
    for req in enriched_spec.requirements_enriched:
        # Simple keyword check across all outputs
        combined = "\n".join(all_outputs.values())
        if req.implied_output and req.implied_output not in combined:
            unmet.append(req.acceptance)

    return {
        "outputs": all_outputs,
        "warnings": warnings,
        "unmet_criteria": unmet,
        "goal": enriched_spec.goal,
    }
```

---

## Phase 6: Orchestrator (Main Entry Point)

Ties everything together in one clean function.

```python
async def run_cowork(
    spec_md_path: str,
    personas: list[Persona],    # registered personas with skill clusters
    skill_configs: dict,        # skill_name → skill config
    llm,
) -> dict:
    """Main orchestrator — the full co-work pipeline."""

    # 1. Read input
    raw_md = Path(spec_md_path).read_text()

    # 2. Detect persona + parse (deterministic detection + 1 LLM call)
    enriched_spec = await persona_parse(raw_md, personas, skill_configs, llm)

    # 3. Plan (1 LLM call — fast model, sees only persona's skills)
    tasks = await plan(enriched_spec, llm)

    # 4. Wire dependencies (deterministic)
    tasks = wire_dependencies(tasks)

    # 5. Execute DAG (1 LLM call per subtask, parallel per wave)
    completed = await execute_dag(tasks, skill_configs, llm)

    # 6. Assemble
    result = await assemble(completed, enriched_spec, llm)

    return result
```

---

## File Structure

```
cowork/
├── persona/
│   ├── models.py              # Persona dataclass
│   ├── registry.py            # PERSONAS config (all persona definitions)
│   └── detector.py            # detect_persona() — keyword scoring
├── parse/
│   ├── models.py              # EnrichedSpec, Component, EnrichedRequirement
│   └── persona_parse.py       # PERSONA_PARSE_PROMPT, persona_parse()
├── plan/
│   ├── models.py              # SubTask, OutputContract
│   └── planner.py             # PLANNER_PROMPT, plan()
├── wire/
│   └── dag.py                 # wire_dependencies(), has_cycle(), group_by_wave()
├── execute/
│   ├── runner.py              # execute_subtask(), execute_dag()
│   ├── validator.py           # validate_contract()
│   └── replan.py              # REPLAN_PROMPT, replan_and_retry()
├── assemble/
│   └── assembler.py           # assemble()
├── orchestrator.py            # run_cowork() — main entry point
└── example_spec.md            # sample input for testing
```

---

## Example Input Spec

```markdown
# Project: Customer Dashboard

## Goal
Build a full-stack customer management dashboard.

## Available Skills
- frontend-design: /skills/frontend/SKILL.md
- api-builder: /skills/api/SKILL.md
- database-query: /skills/db/SKILL.md

## Requirements
1. Create a customers table with columns: id, name, email, status, created_at
2. Build REST API endpoints: GET /customers, GET /customers/:id, POST /customers
3. Build a React dashboard with a table view, search bar, and click-to-detail

## Constraints
- PostgreSQL for database
- FastAPI for API
- Single .jsx file for frontend
```

---

## LLM Call Budget

| Scenario | Parse | Plan | Execute (per task) | Replan | Total |
|----------|-------|------|--------------------|--------|-------|
| 3-task happy path | 1 | 1 | 3 | 0 | **5** |
| 3-task, 1 failure | 1 | 1 | 3 | 1 | **6** |
| 8-task happy path | 1 | 1 | 8 | 0 | **10** |
| 8-task, 2 failures | 1 | 1 | 8 | 2 | **12** |

Setup (registry build) is amortized — runs once per skill change.
