# Implementation Plan — FastAPI + Kafka + Worker + Agent CLI

## Architecture Overview

```
CLIENT (Browser/App)
    │
    │  POST /tasks             (submit a task)
    │  WS /tasks/{id}/watch    (watch progress)
    ▼
FastAPI Service
    │
    │  produces message
    ▼
Kafka Topic: "tasks"
    │
    │  consumed by
    ▼
Worker Service
    │  calls upstream APIs
    │  builds CLI command
    │  spawns subprocess
    ▼
Agent CLI (subprocess)
    │
    │  streams stdout as progress
    ▼
Kafka Topic: "task-progress"
    │
    │  consumed by FastAPI
    ▼
FastAPI fans out to all WebSocket clients watching that task_id
```

---

## Phase 1 — Infrastructure Setup

- Spin up Kafka (Docker Compose for local/beta)
- Create 3 topics: `tasks`, `task-progress`, `task-control`
- Confirm producer/consumer connectivity

---

## Phase 2 — FastAPI Service

### What it does
Owns all client-facing communication. Accepts tasks, manages WebSocket connections, bridges Kafka progress back to clients.

### 2.1 — POST /tasks

```
CLIENT                          FASTAPI
  │                                │
  │── POST /tasks ────────────────>│
  │   { project, type,             │  generate task_id (uuid4)
  │     command?, params }         │  produce → Kafka [tasks topic]
  │<── { task_id, status:queued } ─│
```

### 2.2 — WS /tasks/{id}/watch

```
CLIENT                          FASTAPI                    subscribers{}  history{}
  │                                │                            │              │
  │── WS /tasks/abc/watch ────────>│                            │              │
  │                                │  replay history["abc"]     │              │
  │<── "Starting..." (past) ───────│<───────────────────────────│──────────────│
  │<── "Step 1 done" (past) ───────│                            │              │
  │                                │  create queue              │              │
  │                                │  subscribers["abc"].append(queue)         │
  │                                │                            │              │
  │<── "Step 2 done" (live) ───────│<── queue.get() ────────────│              │
  │<── "DONE" ────────────────────>│                            │              │
  │   WS closes                    │  remove queue from subscribers["abc"]     │
```

> Multiple clients can connect to the same `task_id`. Each gets their own queue, all receive the same updates.

### 2.3 — Background Kafka Consumer (startup)

```
FastAPI starts
  └── asyncio.create_task(consume_progress())  ← runs forever in background

Worker publishes "Step 1 done" to [task-progress]

KAFKA [task-progress]        FASTAPI background consumer
       │                              │
       │── message arrives ──────────>│
                                      │  history["abc"].append("Step 1 done")
                                      │
                                      │  for queue in subscribers["abc"]:
                                      │      queue.put("Step 1 done")
                                      │           │
                                      │           └──> CLIENT 1 gets it
                                      │           └──> CLIENT 2 gets it
                                      │           └──> CLIENT 3 gets it
```

### 2.4 — History Cleanup

```
FASTAPI background consumer receives "DONE"
    │
    │  history["abc"].append("DONE")
    │  fans out to all subscribers
    │
    │  asyncio.sleep(3600)    ← keep history for 1 hour (safety window for late connectors)
    │
    │  history.pop("abc")     ← cleanup
    │  subscribers.pop("abc") ← cleanup
```

---

## Phase 3 — Worker Service

### What it does
Separate service (not inside FastAPI). Pulls one task at a time, calls upstream APIs, executes agent CLI as subprocess, streams progress back, handles cancellation.

### 3.1 — Main Consumer Loop

```
Worker starts
  ├── Thread 1: main task consumer  (tasks topic)
  └── Thread 2: control consumer   (task-control topic)


KAFKA [tasks]            WORKER Thread 1
     │                        │
     │── task "abc" ─────────>│  max_poll_records=1 (one at a time)
                              │
                              │  token = CancellationToken()
                              │  active_tokens["abc"] = token
                              │
                              │  execute_task(task, token)
                              │      │
                              │      │── yields "Starting..."
                              │      │── yields "Step 1 done"
                              │      │── yields "Step 2 done"
                              │      │── yields "DONE"
                              │
                              │  publish each yield → [task-progress]
                              │
                              │  active_tokens.pop("abc")
                              │  consumer.commit()    ← only now
                              │
     │── task "xyz" ─────────>│  pulls next task
```

### 3.2 — CancellationToken

```
class CancellationToken:
    _event = threading.Event()

    cancel()              → _event.set()
    is_cancelled()        → _event.is_set()
    throw_if_cancelled()  → if is_set: raise StopSignal


WORKER Thread 1 (execution)         WORKER Thread 2 (control consumer)
        │                                      │
        │  token = CancellationToken()          │
        │  active_tokens["abc"] = token         │
        │                                      │
        │  execute_task(task, token)            │  KAFKA [task-control] sends STOP
        │     │                                 │       │
        │     │── yield "Starting..."           │       │── consume signal ──────>│
        │     │── throw_if_cancelled() ✓        │       │  task_id = "abc"        │
        │     │── yield "Step 1 done"           │       │  token = active_tokens["abc"]
        │     │── throw_if_cancelled() ✓        │       │  token.cancel()  ← sets flag
        │     │── yield "Step 2 done"           │
        │     │── throw_if_cancelled() ✗ STOP  │
        │          ↓ StopSignal raised           │
        │  catch StopSignal                     │
        │  publish "STOPPED"                    │
        │  consumer.commit()                    │
```

### 3.3 — Offset Commit Guarantee

```
WORKER
  │
  │  consume task "abc"
  │       │
  │       │  offset NOT committed yet ← Kafka still owns this
  │       │
  │       │  execute...
  │       │
  │       ├── success    → publish "DONE"    → commit offset ✓
  │       ├── StopSignal → publish "STOPPED" → commit offset ✓
  │       └── crash      → NO commit
  │                           │
  │                           └── Kafka re-delivers task "abc"
  │                               to next available worker ← reliability guarantee
```

---

## Phase 4 — Agent CLI Wiring

### What it does
The Worker calls upstream APIs to fetch data, builds CLI arguments from the response, spawns the agent as a subprocess, and streams its stdout as progress.

### 4.1 — Full Flow: Kafka → API → CLI → Progress

```
STEP 1 — Worker pulls task from Kafka

KAFKA [tasks]              WORKER
     │                       │
     │── task "abc" ────────>│
                             │  task = { task_id, project, type, command?, params }

Three task types the client can submit:

  ┌─ type: "seed-db" ───────────────────────────────────────────┐
  │  {                                                          │
  │    task_id: "abc",                                          │
  │    project: "default",                                      │
  │    type: "seed-db",                                         │
  │    params: { reset: true }                                  │
  │  }                                                          │
  └─────────────────────────────────────────────────────────────┘

  ┌─ type: "task-context" ──────────────────────────────────────┐
  │  {                                                          │
  │    task_id: "def",                                          │
  │    project: "default",                                      │
  │    type: "task-context",                                    │
  │    command: "import-excel",                                 │
  │    params: {                                                │
  │      file_path: "/data/uploads/report.xlsx",                │
  │      main_tab: "MB Attributes",                             │
  │      custom_structure: true                                 │
  │    }                                                        │
  │  }                                                          │
  └─────────────────────────────────────────────────────────────┘

  ┌─ type: "agent" ─────────────────────────────────────────────┐
  │  {                                                          │
  │    task_id: "ghi",                                          │
  │    project: "default",                                      │
  │    type: "agent",                                           │
  │    command: "run",                                          │
  │    params: {                                                │
  │      input: "Analyze all MB attributes for Q1",             │
  │      parallel_workers: 3                                    │
  │    }                                                        │
  │  }                                                          │
  └─────────────────────────────────────────────────────────────┘


STEP 2 — Worker fetches project config (single API call)

WORKER                      PROJECT CONFIG API
  │                               │
  │── GET /projects/default ─────>│
  │<── { db_conn, paths, env } ───│
  │
  │  worker now has project context + task params from Kafka
  │  (no second API call needed — Kafka message already carries all task-specific params)


STEP 3 — Worker builds CLI command from task type + params

WORKER
  │
  │  cli_args = cli_builder.build(task)
  │
  │  ┌─ seed-db ────────────────────────────────────────────┐
  │  │  ["python", "agent.py", "seed-db", "--reset"]        │
  │  └──────────────────────────────────────────────────────┘
  │
  │  ┌─ task-context ───────────────────────────────────────┐
  │  │  ["python", "agent.py", "task-context",              │
  │  │   "import-excel", "/data/uploads/report.xlsx",       │
  │  │   "--main-tab", "MB Attributes",                     │
  │  │   "--custom-structure"]                               │
  │  └──────────────────────────────────────────────────────┘
  │
  │  ┌─ agent ──────────────────────────────────────────────┐
  │  │  ["python", "agent.py", "agent", "run",              │
  │  │   "--input", "Analyze all MB attributes for Q1",     │
  │  │   "--parallel-workers", "3"]                          │
  │  └──────────────────────────────────────────────────────┘


STEP 4 — Worker spawns CLI subprocess and streams output

WORKER                           AGENT CLI
  │                                  │
  │  proc = subprocess.Popen(        │
  │    cli_args, stdout=PIPE         │
  │  )                               │
  │                                  │
  │  for line in proc.stdout:        │<── "Initializing..."
  │      token.throw_if_cancelled()  │<── "Loaded config"
  │      yield line                  │<── "Processing row 1/500"
  │                                  │<── "DONE"
  │
  │  publish each line → [task-progress]
```

### 4.2 — Worker File Structure

```
worker/
  ├── main.py          ← consumer loop, calls execute_task()
  ├── executor.py      ← execute_task(), subprocess management, token checks
  ├── api_client.py    ← calls upstream APIs, returns structured data
  └── cli_builder.py   ← takes task params + API data → builds CLI args list
```

### 4.3 — executor.py Responsibilities

```python
def execute_task(task, token):
    # 1. fetch project config (single API call)
    project_config = api_client.get_project_config(task["project"])

    # 2. build CLI command from task type + params
    cli_args = cli_builder.build(task, project_config)
    #   seed-db      → ["python", "agent.py", "seed-db", "--reset"]
    #   task-context  → ["python", "agent.py", "task-context", "import-excel", path, "--main-tab", ...]
    #   agent         → ["python", "agent.py", "agent", "run", "--input", text, "--parallel-workers", n]

    # 3. spawn subprocess, stream output
    proc = subprocess.Popen(cli_args, stdout=PIPE, stderr=PIPE)

    for line in proc.stdout:
        token.throw_if_cancelled()    # cancel check on every line
        yield line.decode().strip()

    if proc.returncode != 0:
        raise TaskFailed(proc.stderr.read())
```

### 4.4 — Ownership Summary

| Who | Does What |
|---|---|
| Client | Sends project + type + params (e.g. seed-db --reset, import-excel path, agent run --input) |
| FastAPI | Stores them as-is in Kafka, no enrichment |
| Worker | Pulls task, fetches project config (1 API call), builds CLI args |
| Agent CLI | Receives complete ready-to-run args, executes, streams stdout |

---

## Phase 5 — Stop / Cancel Flow

### Full end-to-end cancel sequence

```
STEP 1 — Task is running, client is watching

CLIENT          FASTAPI          KAFKA          WORKER          AGENT CLI
  │                │               │               │                │
  │<── "Starting" ─│<── consume ───│<── publish ───│<── stdout ─────│
  │<── "Step 1" ───│               │               │                │
  │<── "Step 2" ───│               │               │  (running...)  │


STEP 2 — Client sends STOP over WebSocket

CLIENT          FASTAPI
  │                │
  │── "STOP" ─────>│
                   │  produce → [task-control]
                   │  { task_id: "abc", signal: "STOP" }


STEP 3 — Worker Thread 2 picks up the stop signal

KAFKA [task-control]        WORKER Thread 2
       │                          │
       │── signal "abc:STOP" ────>│
                                  │  active_tokens["abc"].cancel()
                                  │  ← sets threading.Event flag


STEP 4 — Worker Thread 1 hits next checkpoint

WORKER Thread 1
  │
  │  (inside execute_task loop, reading stdout line by line)
  │  token.throw_if_cancelled()   ← checks flag
  │       ↓ flag is SET
  │  proc.kill()                  ← kills agent CLI subprocess
  │  proc.wait()
  │  raise StopSignal
  │
  │  catch StopSignal
  │  publish "STOPPED" → [task-progress]
  │  active_tokens.pop("abc")
  │  consumer.commit()


STEP 5 — STOPPED flows back to client

KAFKA [task-progress]    FASTAPI background consumer     CLIENT
       │                          │                        │
       │── "STOPPED" ────────────>│                        │
                                  │  history["abc"].append │
                                  │  queue.put("STOPPED")  │
                                  │── "STOPPED" ──────────>│
                                                           │
                                                  WS closes
```

### Checkpoint granularity — how fast cancellation responds

```
COARSE checkpoints (slow to respond):

  execute_task:
    do_phase_1()                    ← takes 30 seconds, no checkpoint inside
    token.throw_if_cancelled()      ← only checked after phase 1 completes
    do_phase_2()

  STOP sent → up to 30 seconds before it takes effect


FINE checkpoints (fast to respond):

  execute_task:
    for line in proc.stdout:
        token.throw_if_cancelled()  ← checked on EVERY line of CLI output
        yield line

  STOP sent → takes effect on the very next stdout line from CLI (milliseconds)
```

> Since the agent CLI streams output line by line, checking the token on every line gives near-instant cancellation with no changes needed to the CLI itself.

---

## Phase 6 — End-to-End Testing

- Single client happy path: POST → WS → stream → DONE
- Late client: connect after task started, verify history replay works
- Multiple clients: two clients watching the same `task_id`
- Cancel mid-execution: send STOP, verify subprocess dies cleanly
- Worker crash recovery: kill worker mid-task, verify Kafka re-delivers

---

## Phase 7 — Hardening for Beta

- Structured logging on all 3 services
- Task status endpoint `GET /tasks/{id}` — polling fallback
- Dead letter handling — what happens if worker fails repeatedly on same task
- Temp file cleanup — remove `/tmp/task_abc.json` files after task completes
- Docker Compose — wire FastAPI + Worker + Kafka together for local dev

---

## Kafka Topics Reference

| Topic | Producer | Consumer | Message Shape |
|---|---|---|---|
| `tasks` | FastAPI | Worker Thread 1 | `{ task_id, project, type, command?, params }` |
| `task-progress` | Worker | FastAPI background | `{ task_id, update }` |
| `task-control` | FastAPI | Worker Thread 2 | `{ task_id, signal: "STOP" }` |
