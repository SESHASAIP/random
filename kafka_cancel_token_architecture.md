# Kafka Cancel Token Architecture

## Overview

A distributed task cancellation system using two Kafka topics and an in-memory CancelToken pattern.
The API service (Repo A) and the Consumer Worker service (Repo B) are completely separate processes.
Cancellation signals cross the process boundary via a dedicated Kafka topic.

---

## High-Level Architecture

```mermaid
graph TB
    Client(["👤 Client"])
    API["API Service\n(Repo A)\nFastAPI"]
    KT["Kafka\ntask-events"]
    KC["Kafka\ntask-cancels"]
    Consumer["Consumer Worker\n(Repo B)"]
    Pool["ThreadPoolExecutor\nWorker Pool"]
    Token["CancelToken\n(in-memory)"]
    Registry["TokenRegistry\n(in-memory)"]
    StatusStore[("Redis\nTask Status")]

    Client -->|"POST /tasks/trigger"| API
    Client -->|"DELETE /tasks/:id/cancel"| API

    API -->|"start event"| KT
    API -->|"cancel event"| KC

    KT -->|"polls"| Consumer
    KC -->|"polls"| Consumer

    Consumer -->|"submit(run_task, token)"| Pool
    Consumer -->|"token.cancel()"| Registry
    Registry -->|"finds token"| Token
    Token -->|"_cancelled = True"| Pool

    Pool -->|"writes result"| StatusStore
    Client -->|"GET /tasks/:id/status"| StatusStore
```

---

## Two Kafka Topics — Why?

```mermaid
graph LR
    subgraph "❌ Same Topic — Cancel Stuck in Backlog"
        T1["task-events\n[start:aaa][start:bbb][start:ccc][cancel:abc123][start:ddd]"]
        T1 -->|"consumer processes in order"| W1["cancel waits\nbehind 3 events ❌"]
    end

    subgraph "✅ Separate Topics — Cancel Processed Immediately"
        T2["task-events\n[start:aaa][start:bbb][start:ccc][start:ddd]"]
        T3["task-cancels\n[cancel:abc123]"]
        T2 --> W2["start events\nprocessed normally"]
        T3 -->|"processed immediately"| W3["cancel fires\nright away ✅"]
    end
```

---

## Process Boundary — How Signal Crosses Repos

```mermaid
sequenceDiagram
    participant Client
    participant API as API Service (Repo A)
    participant Kafka as Kafka: task-cancels
    participant Consumer as Consumer Worker (Repo B)
    participant Thread as Worker Thread

    Client->>API: DELETE /tasks/abc123/cancel
    API->>Kafka: produce {action: cancel, task_id: abc123}
    Note over API: API has NO CancelToken<br/>it just sends the signal

    Kafka->>Consumer: poll → cancel event received
    Consumer->>Consumer: token = token_registry["abc123"]
    Consumer->>Thread: token.cancel() → _cancelled = True

    Note over Thread: At next checkpoint:<br/>token.check() raises CancelledError
    Thread->>Thread: CancelledError → stops cleanly
    Thread->>Consumer: status = CANCELLED
```

---

## CancelToken Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Created: Consumer receives start event
    Created --> Registered: token_registry[task_id] = token
    Registered --> Queued: executor.submit(run_task, token)
    Queued --> PreCancelled: cancel arrives before task starts
    PreCancelled --> Cancelled: token already cancelled → run_task exits immediately
    Queued --> Running: worker thread picks up task
    Running --> Cancelled: token.cancel() called → thread hits check()
    Running --> Success: task completes normally
    Running --> Failed: unhandled exception
    Cancelled --> Cleanup: registry.release(task_id)
    Success --> Cleanup
    Failed --> Cleanup
    Cleanup --> [*]
```

---

## Ordering Edge Case — Cancel Arrives Before Start

```mermaid
sequenceDiagram
    participant Kafka as Kafka: task-cancels
    participant Kafka2 as Kafka: task-events
    participant Consumer

    Kafka->>Consumer: cancel:abc123 arrives FIRST
    Note over Consumer: token_registry["abc123"] = None<br/>token not created yet!

    Consumer->>Consumer: _pre_cancelled.add("abc123")
    Note over Consumer: remember it for later

    Kafka2->>Consumer: start:abc123 arrives
    Consumer->>Consumer: token = CancelToken()

    Note over Consumer: check pre-cancelled set!
    Consumer->>Consumer: token.cancel() immediately
    Consumer->>Consumer: token_registry["abc123"] = token

    Consumer->>Consumer: executor.submit(run_task, token)
    Note over Consumer: task starts but token<br/>already cancelled → exits immediately ✅
```

---

## Worker Thread — Cooperative Cancellation

```mermaid
flowchart TD
    Start(["run_task() called"])
    PreCheck{"token.is_cancelled?\n(pre-run check)"}
    MarkRunning["status = RUNNING"]
    Step1["step 1: fetch data"]
    Check1{"token.check()"}
    Step2["step 2: build report"]
    Check2{"token.check()"}
    Step3["step 3: upload to S3"]
    Check3{"token.check()"}
    Done["status = SUCCESS"]
    Cancelled["status = CANCELLED\nregistry.release()"]
    Failed["status = FAILED"]

    Start --> PreCheck
    PreCheck -->|"yes"| Cancelled
    PreCheck -->|"no"| MarkRunning
    MarkRunning --> Step1
    Step1 --> Check1
    Check1 -->|"cancelled"| Cancelled
    Check1 -->|"ok"| Step2
    Step2 --> Check2
    Check2 -->|"cancelled"| Cancelled
    Check2 -->|"ok"| Step3
    Step3 --> Check3
    Check3 -->|"cancelled"| Cancelled
    Check3 -->|"ok"| Done
    Step1 -->|"exception"| Failed
    Step2 -->|"exception"| Failed
    Step3 -->|"exception"| Failed
```

---

## Consumer Main Loop

```mermaid
flowchart TD
    Poll(["poll Kafka\ntask-events + task-cancels"])
    Topic{"which topic?"}

    CancelFlow["find token in registry"]
    HasToken{"token found?"}
    CancelToken_["token.cancel()"]
    PreCancel["_pre_cancelled.add(task_id)"]

    StartFlow["create CancelToken()"]
    WasPreCancelled{"in _pre_cancelled?"}
    CancelNow["token.cancel() immediately"]
    RegisterToken["token_registry[task_id] = token"]
    Submit["executor.submit(run_task, event, token)"]
    BackPressure{"pool queue\n> MAX_BACKLOG?"}
    PauseKafka["pause Kafka"]
    ResumeKafka["resume Kafka"]

    Poll --> Topic
    Topic -->|"task-cancels"| CancelFlow
    Topic -->|"task-events"| BackPressure

    CancelFlow --> HasToken
    HasToken -->|"yes"| CancelToken_
    HasToken -->|"no"| PreCancel
    CancelToken_ --> Poll
    PreCancel --> Poll

    BackPressure -->|"yes"| PauseKafka
    PauseKafka -->|"wait for drain"| ResumeKafka
    ResumeKafka --> StartFlow
    BackPressure -->|"no"| StartFlow

    StartFlow --> WasPreCancelled
    WasPreCancelled -->|"yes"| CancelNow
    WasPreCancelled -->|"no"| RegisterToken
    CancelNow --> RegisterToken
    RegisterToken --> Submit
    Submit --> Poll
```

---

## Deployment — Two Separate Services

```mermaid
graph TB
    subgraph "OpenShift / Docker"
        subgraph "API Pod (Repo A)"
            FastAPI["FastAPI\nuvicorn api:app"]
            Producer["KafkaProducer"]
            FastAPI --> Producer
        end

        subgraph "Worker Pod (Repo B) — scale independently"
            ConsumerLoop["consumer.py\npython consumer.py"]
            ThreadPool["ThreadPoolExecutor\nMAX_WORKERS=10"]
            TR["TokenRegistry\n(in-memory)"]
            ConsumerLoop --> ThreadPool
            ConsumerLoop --> TR
        end

        subgraph "Kafka"
            TE["topic: task-events"]
            TC["topic: task-cancels"]
        end

        subgraph "Redis"
            StatusDB["task status store"]
        end

        Producer -->|"start events"| TE
        Producer -->|"cancel events"| TC
        TE -->|"poll"| ConsumerLoop
        TC -->|"poll"| ConsumerLoop
        ThreadPool -->|"write status"| StatusDB
        FastAPI -->|"read status"| StatusDB
    end
```

---

## Task Status State Machine

```mermaid
stateDiagram-v2
    [*] --> PENDING: start event consumed
    PENDING --> RUNNING: worker thread starts
    PENDING --> CANCELLED: cancel arrives while queued\n(pre-run check catches it)
    RUNNING --> SUCCESS: task completes
    RUNNING --> FAILED: unhandled exception
    RUNNING --> CANCELLED: token.check() raises CancelledError
    SUCCESS --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

---

## Component Responsibilities

| Component | Lives In | Responsibility |
|---|---|---|
| `api.py` | Repo A | Receive HTTP requests, produce Kafka events |
| `KafkaProducer` | Repo A | Send start/cancel events to correct topics |
| `consumer.py` | Repo B | Poll both topics, route start vs cancel |
| `CancelToken` | Repo B (in-memory) | Flag object shared between consumer loop and worker thread |
| `TokenRegistry` | Repo B (in-memory) | Map task_id → live CancelToken |
| `tasks.py` | Repo B | Business logic with `token.check()` checkpoints |
| `Redis` | Shared | Task status store readable by both repos |
| `task-events` topic | Kafka | Carries start payloads |
| `task-cancels` topic | Kafka | Carries cancel signals — processed with priority |

---

## Key Rules

1. **CancelToken never leaves Repo B** — it is purely in-memory within the consumer process
2. **Kafka is the only bridge** between Repo A and Repo B
3. **Partition key = task_id** on producer — ensures ordering per task
4. **Always call `token.check()`** at the start of `run_task()` to catch pre-cancelled tasks
5. **`_pre_cancelled` set** handles the race where cancel arrives before start
6. **`registry.release()`** in `finally` block — always clean up tokens
7. **Scale worker pods freely** — each pod has its own in-memory registry, Kafka handles partition assignment
