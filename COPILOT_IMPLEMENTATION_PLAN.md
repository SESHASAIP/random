# Copilot Implementation Plan
## Resumable ADK Agent: Cold-Start Context Management

> **For the AI coding assistant**: This document is your spec. Work through tasks in order. Each task has explicit deliverables, acceptance criteria, and code targets. Do not skip ahead. Confirm completion of each task before moving to the next.

---

## 0. Context & Glossary

**Stack**:
- Python 3.11+
- `google-adk == 1.21.*` (pinned)
- PostgreSQL 14+ via `DatabaseSessionService`
- OpenTelemetry for metrics
- pytest + pytest-asyncio for tests

**Key terms**:
- **Cold start / resume**: a request arriving with an existing `session_id` after the previous process has exited or the session has been dormant.
- **Hydration**: the act of `get_session()` loading session state and events from Postgres.
- **Cap / bound**: limiting the number of events returned by `get_session()` so the LLM call that follows doesn't overflow the context window.
- **`GetSessionConfig`**: ADK's built-in dataclass that accepts `num_recent_events: int | None` to limit hydration. We use this as our underlying mechanism.

**Architectural principle (do not violate)**:
- The Postgres `events` table is **append-only and never trimmed by our code**. Audit trail is sacred.
- We bound only the **in-memory** view returned by `get_session()`.
- We do **not** modify ADK internals, plugins, or the `_build_contents` pipeline. We only subclass `DatabaseSessionService`.

---

## 1. Problem Statement (one paragraph for context)

On cold start of a long-running ADK session (e.g., 5,000+ events accumulated over days), the default `DatabaseSessionService.get_session()` rehydrates the full event log into memory. ADK's runner then builds `llm_request.contents` from those events, which can exceed the model's context window and crash the first LLM call after resume. ADK's active-run mechanisms (`EventsCompactionConfig`, `ContextFilterPlugin`) only operate after hydration. We need to cap hydration at the SessionService layer so that cold-start prompts are the same size as mid-run prompts — without touching the durable event log or breaking admin/audit consumers that legitimately need full reads.

---

## 2. Deliverables

A new Python package `resumable/` under the project's `src/` directory containing:

1. `resumable/__init__.py` — exports `ResumableDatabaseSessionService`, `UNBOUNDED`
2. `resumable/session_service.py` — main class
3. `resumable/observability.py` — OpenTelemetry counter + structured logging helper
4. `tests/test_resumable_session.py` — unit tests
5. `tests/test_integration.py` — integration test against a real Postgres
6. `docs/resumable-session-service.md` — operator-facing runbook

Existing files modified:
- `pyproject.toml` (or `requirements.txt`) — pin `google-adk==1.21.*`
- Application bootstrap module — swap `DatabaseSessionService` for `ResumableDatabaseSessionService`

---

## 3. Task Breakdown

Work through tasks in this exact order. Each task lists files to create/modify, expected lines of code, and acceptance criteria.

---

### Task 1: Project scaffolding

**Goal**: Create the package skeleton and pin the ADK version.

**Files to create**:
- `src/resumable/__init__.py` (empty for now)
- `src/resumable/session_service.py` (empty for now)
- `src/resumable/observability.py` (empty for now)
- `tests/__init__.py` (empty)

**Files to modify**:
- `pyproject.toml`: ensure `google-adk = "==1.21.*"` is in dependencies. Add `opentelemetry-api`, `opentelemetry-sdk` if not present.

**Acceptance criteria**:
- `pip install -e .` succeeds
- `python -c "import resumable"` succeeds (even though it exports nothing yet)
- `pip show google-adk` reports a version in the 1.21.x range

---

### Task 2: Observability primitive

**Goal**: Implement the OpenTelemetry counter and structured log helper used by the session service.

**File**: `src/resumable/observability.py`

**Implementation spec**:

```python
"""Observability helpers for ResumableDatabaseSessionService.

Exposes a counter for cap-hit events and a structured log helper.
Both are safe to call from async contexts.
"""
import logging
from typing import Optional

from opentelemetry import metrics

_logger = logging.getLogger("resumable.session_service")

_meter = metrics.get_meter(__name__)
_cap_hit_counter = _meter.create_counter(
    name="resumable_session.cap_hit",
    description=(
        "Count of get_session() calls where the cold-start event cap was "
        "applied AND the cap actually bit (returned events == cap)."
    ),
    unit="1",
)


def record_cap_hit(*, app_name: str) -> None:
    """Increment the cap-hit counter for an app."""
    _cap_hit_counter.add(1, attributes={"app_name": app_name})


def log_cap_applied(
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    cap: int,
    events_returned: int,
) -> None:
    """Structured INFO log when the cap is applied and bites."""
    _logger.info(
        "cold_start_cap_applied",
        extra={
            "app_name": app_name,
            "user_id": user_id,
            "session_id": session_id,
            "cap": cap,
            "events_returned": events_returned,
        },
    )
```

**Acceptance criteria**:
- File compiles and imports cleanly
- `from resumable.observability import record_cap_hit, log_cap_applied` works
- Calling `record_cap_hit(app_name="test")` does not raise (even if no exporter is configured)

---

### Task 3: Core session service

**Goal**: Implement `ResumableDatabaseSessionService` and the `UNBOUNDED` sentinel.

**File**: `src/resumable/session_service.py`

**Implementation spec**:

```python
"""Resumable ADK session service.

Subclasses DatabaseSessionService to apply a cold-start hydration cap
on default reads, while preserving full reads for callers that opt out.

The Postgres events table is never modified by this class. Only the
in-memory Session.events list returned by get_session() is bounded.
"""
from typing import Optional, Set

from google.adk.sessions import DatabaseSessionService
from google.adk.sessions.session import Session
from google.adk.sessions.base_session_service import GetSessionConfig

from resumable.observability import log_cap_applied, record_cap_hit


class _Unbounded:
    """Sentinel GetSessionConfig that disables the cold-start cap.

    Pass `config=UNBOUNDED` to get_session() to force a full read,
    bypassing the configured cap. Use for admin tooling, audit
    pipelines, and Kafka consumers that need full session history.
    """

    _instance: Optional["_Unbounded"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNBOUNDED"


UNBOUNDED = _Unbounded()


class ResumableDatabaseSessionService(DatabaseSessionService):
    """DatabaseSessionService with a cold-start event cap.

    Default get_session() calls (e.g., from the ADK runner) are capped
    at `cold_start_event_cap` events. Callers can override by passing
    an explicit GetSessionConfig, or bypass entirely with `config=UNBOUNDED`.

    Callers in `bypass_for_user_ids` are never capped (intended for
    admin user IDs).

    The cap exists to bound the size of llm_request.contents on the
    first turn after a session is resumed. Postgres still contains the
    full event log.
    """

    def __init__(
        self,
        *args,
        cold_start_event_cap: int = 100,
        bypass_for_user_ids: Optional[Set[str]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if cold_start_event_cap <= 0:
            raise ValueError("cold_start_event_cap must be positive")
        self._cap = cold_start_event_cap
        self._bypass_users: Set[str] = bypass_for_user_ids or set()

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        effective_config, cap_was_applied = self._resolve_config(config, user_id)

        session = await super().get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            config=effective_config,
        )

        if session is not None and cap_was_applied:
            self._maybe_emit_cap_hit(app_name, user_id, session_id, session)

        return session

    def _resolve_config(
        self,
        caller_config: Optional[GetSessionConfig],
        user_id: str,
    ) -> tuple[Optional[GetSessionConfig], bool]:
        """Decide whether to apply the cap.

        Returns (effective_config_to_pass_to_super, cap_was_applied).

        Decision table:
          caller passes UNBOUNDED        → no cap (returns None config)
          user_id in bypass set          → no cap (passes caller's config through)
          caller passes explicit config  → no cap (passes caller's config through)
          caller passes None             → cap applied (returns GetSessionConfig with our cap)
        """
        if isinstance(caller_config, _Unbounded):
            return None, False
        if user_id in self._bypass_users:
            return caller_config, False
        if caller_config is not None:
            return caller_config, False
        return GetSessionConfig(num_recent_events=self._cap), True

    def _maybe_emit_cap_hit(
        self,
        app_name: str,
        user_id: str,
        session_id: str,
        session: Session,
    ) -> None:
        """Emit observability only when the cap actually bit.

        A cap of 100 on a session with 30 events is not a 'hit'.
        Only emit when returned == cap, which signals there were
        likely more events in the DB we did not load.
        """
        events_returned = len(session.events)
        if events_returned < self._cap:
            return

        log_cap_applied(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            cap=self._cap,
            events_returned=events_returned,
        )
        record_cap_hit(app_name=app_name)
```

**Acceptance criteria**:
- File compiles
- `from resumable.session_service import ResumableDatabaseSessionService, UNBOUNDED` works
- `ResumableDatabaseSessionService(cold_start_event_cap=0)` raises `ValueError`
- `ResumableDatabaseSessionService(cold_start_event_cap=-1)` raises `ValueError`
- `UNBOUNDED is UNBOUNDED` returns `True` (singleton)

---

### Task 4: Package exports

**Goal**: Make the public API clean.

**File**: `src/resumable/__init__.py`

**Implementation spec**:

```python
"""Resumable ADK session service.

Public API:
  ResumableDatabaseSessionService -- subclass of DatabaseSessionService
                                     that applies a cold-start hydration cap.
  UNBOUNDED                       -- sentinel to bypass the cap.

Example:
  from resumable import ResumableDatabaseSessionService, UNBOUNDED

  service = ResumableDatabaseSessionService(
      db_url="postgresql+asyncpg://...",
      cold_start_event_cap=100,
  )

  # Runner path -- cap applies:
  session = await service.get_session(
      app_name="my-app", user_id="u1", session_id="s1"
  )

  # Admin path -- no cap:
  full_session = await service.get_session(
      app_name="my-app", user_id="u1", session_id="s1",
      config=UNBOUNDED,
  )
"""
from resumable.session_service import ResumableDatabaseSessionService, UNBOUNDED

__all__ = ["ResumableDatabaseSessionService", "UNBOUNDED"]
```

**Acceptance criteria**:
- `from resumable import ResumableDatabaseSessionService, UNBOUNDED` works at the package level

---

### Task 5: Unit tests

**Goal**: Comprehensive unit coverage of the decision logic and observability.

**File**: `tests/test_resumable_session.py`

**Test framework**: pytest + pytest-asyncio. Use `unittest.mock.AsyncMock` to mock the parent `DatabaseSessionService.get_session`.

**Required test cases** (implement all):

| Test name | Behavior under test |
|---|---|
| `test_init_rejects_zero_cap` | `cold_start_event_cap=0` raises `ValueError` |
| `test_init_rejects_negative_cap` | `cold_start_event_cap=-5` raises `ValueError` |
| `test_default_call_applies_cap` | `get_session()` with no config → super called with `GetSessionConfig(num_recent_events=cap)` |
| `test_unbounded_sentinel_disables_cap` | `get_session(config=UNBOUNDED)` → super called with `config=None` |
| `test_explicit_config_honored` | `get_session(config=GetSessionConfig(num_recent_events=30))` → super called with caller's exact config (not mutated) |
| `test_bypass_user_skips_cap` | User in `bypass_for_user_ids` → super called with caller's config unchanged |
| `test_caller_config_not_mutated` | Caller's config object's `num_recent_events` is not changed after the call |
| `test_cap_hit_metric_when_full` | When returned events count == cap → `record_cap_hit` called once |
| `test_no_cap_hit_metric_when_below` | When returned events count < cap → `record_cap_hit` NOT called |
| `test_no_metric_for_unbounded` | UNBOUNDED path → no metric emitted regardless of event count |
| `test_no_metric_for_bypass_user` | Bypass user path → no metric emitted |
| `test_none_session_returned_safely` | When super returns `None`, our wrapper returns `None` without crashing |
| `test_unbounded_is_singleton` | `UNBOUNDED is _Unbounded()` → True |

**Skeleton** (Copilot: implement all tests in this style):

```python
from unittest.mock import AsyncMock, patch
import pytest
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.session import Session
from google.adk.events.event import Event

from resumable import ResumableDatabaseSessionService, UNBOUNDED


def _make_session(num_events: int) -> Session:
    """Helper: build a Session with N dummy events."""
    return Session(
        id="test-session",
        app_name="test-app",
        user_id="test-user",
        state={},
        events=[
            Event(invocation_id=f"inv-{i}", author="user")
            for i in range(num_events)
        ],
        last_update_time=0.0,
    )


@pytest.fixture
def service():
    # We don't actually connect to a DB in unit tests; we mock super().get_session.
    svc = ResumableDatabaseSessionService.__new__(ResumableDatabaseSessionService)
    svc._cap = 100
    svc._bypass_users = set()
    return svc


@pytest.mark.asyncio
async def test_default_call_applies_cap(service):
    captured = {}

    async def fake_super(*, app_name, user_id, session_id, config):
        captured["config"] = config
        return _make_session(50)

    with patch.object(
        ResumableDatabaseSessionService.__mro__[1],
        "get_session",
        new=fake_super,
    ):
        await service.get_session(
            app_name="a", user_id="u", session_id="s",
        )

    assert captured["config"] is not None
    assert captured["config"].num_recent_events == 100


# ... implement remaining 12 test cases following this pattern ...
```

**Acceptance criteria**:
- All 13 listed tests exist and pass
- `pytest tests/test_resumable_session.py -v` runs green
- Coverage of `session_service.py` is ≥ 95%

---

### Task 6: Integration test

**Goal**: Verify the full path against a real Postgres-backed `DatabaseSessionService`.

**File**: `tests/test_integration.py`

**Setup**: Use `testcontainers-python` to spin up an ephemeral Postgres container, or a `docker-compose` service that's started before tests. Document whichever you choose at the top of the file.

**Required test cases**:

1. **`test_resume_long_session_returns_capped_events`**:
   - Create a session, append 500 events via `session_service.append_event()`.
   - Call `get_session()` with no config.
   - Assert returned `session.events` has length == cap.
   - Query Postgres directly: assert `events` table has 500 rows for the session (durability check).

2. **`test_unbounded_returns_all`**:
   - Same setup (500 events).
   - Call `get_session(config=UNBOUNDED)`.
   - Assert returned `session.events` has length == 500.

3. **`test_small_session_under_cap`**:
   - Create a session with 20 events.
   - Call `get_session()` with no config.
   - Assert returned `session.events` has length == 20 (the cap of 100 is not bitten).

4. **`test_cap_hit_metric_observable`**:
   - Configure an in-memory OpenTelemetry metrics reader.
   - Create a session with 200 events.
   - Call `get_session()` with cap=100.
   - Assert the `resumable_session.cap_hit` counter incremented exactly once.

**Acceptance criteria**:
- All 4 integration tests pass against a real Postgres instance
- Tests clean up their fixtures (delete the test sessions/events on teardown)

---

### Task 7: Application bootstrap migration

**Goal**: Swap the production session service.

**Files to modify**: locate the application's runner bootstrap (the file where `DatabaseSessionService(...)` is currently instantiated and passed to `Runner`). This is typically named something like `app.py`, `main.py`, or `bootstrap.py`.

**Migration**:

```python
# Before:
from google.adk.sessions import DatabaseSessionService

session_service = DatabaseSessionService(
    db_url=os.environ["POSTGRES_DSN"],
)

# After:
from resumable import ResumableDatabaseSessionService

session_service = ResumableDatabaseSessionService(
    db_url=os.environ["POSTGRES_DSN"],
    cold_start_event_cap=int(os.environ.get("ADK_COLD_START_CAP", "100")),
    bypass_for_user_ids=set(
        os.environ.get("ADK_BYPASS_USER_IDS", "").split(",")
    ) - {""},
)
```

**Environment variables to document**:
- `ADK_COLD_START_CAP` — integer, default 100
- `ADK_BYPASS_USER_IDS` — comma-separated list of user IDs to bypass the cap (e.g., `admin,audit-bot`)

**Acceptance criteria**:
- The application starts with the new service in place
- All existing agent flow tests still pass
- The runner accepts the new service without modification (it's a drop-in subclass)

---

### Task 8: Audit/Kafka consumer migration

**Goal**: Update any code paths that legitimately need full session reads.

**Action**: Search the codebase for calls to `session_service.get_session(`. For each call site, decide:

- **Runner path (typical)**: leave as-is. The cap should apply.
- **Audit / Kafka / admin path**: change to `session_service.get_session(..., config=UNBOUNDED)`.

**Common sites to check**:
- Kafka consumer worker that processes session completions
- Any admin API endpoint that returns session details
- Any reporting/analytics pipeline
- Any debugging utility

**Acceptance criteria**:
- Code review identifies every `get_session` call site
- Each site is documented as "runner" or "audit" with a one-line justification
- Audit sites use `UNBOUNDED`; runner sites do not

---

### Task 9: Operator runbook

**Goal**: Document operational behavior so on-call engineers can act on alerts.

**File**: `docs/resumable-session-service.md`

**Required sections**:

1. **What this service does** — one paragraph
2. **How to tune `ADK_COLD_START_CAP`** — measurement methodology (run a representative session, count events / invocations, pick cap = 20 × ratio)
3. **The cap-hit metric** — what it means, what threshold should trigger investigation
4. **Bypassing the cap** — how to use `UNBOUNDED`, when to add a user to `ADK_BYPASS_USER_IDS`
5. **Rollback procedure** — replace `ResumableDatabaseSessionService` with `DatabaseSessionService` in bootstrap; no data migration required
6. **Known limitations** — never-compacted sessions can lose old context on resume; this is by design and documented

**Acceptance criteria**:
- File exists with all six sections
- Reviewed by one team member outside the implementer

---

### Task 10: Rollout

**Goal**: Deploy in three phases with explicit go/no-go gates.

**Phase 1 — Shadow** (1 day):
- Deploy with `ADK_COLD_START_CAP=10000` (effectively unbounded).
- Confirm metric `resumable_session.cap_hit` reports zero.
- Confirm no behavior change vs. baseline.
- **Gate**: zero cap hits, zero regressions in agent quality metrics.

**Phase 2 — Active on one agent** (2 days observation):
- Reduce `ADK_COLD_START_CAP` to the tuned value (computed per Task 9 methodology).
- Apply to one production agent.
- Monitor:
  - Cap-hit rate (target: < 5% of resume calls)
  - First-turn-after-resume p50/p95 latency (expect drop)
  - First-turn token cost (expect drop)
  - Agent quality: response coherence, tool-call success rate (must not regress)
- **Gate**: latency drops, quality stable, cap-hit rate acceptable.

**Phase 3 — Full rollout** (1 day):
- Apply tuned cap to all agents.
- Update runbook with observed cap-hit rates.

**Acceptance criteria**:
- All three phases completed without rollback
- Final cap value documented in the runbook with measurement data

---

## 4. Non-Goals (Do Not Implement)

Explicitly listed so Copilot does not scope-creep:

- ❌ Do not build a sidecar `compaction_index` table
- ❌ Do not subclass or wrap `LlmEventSummarizer`
- ❌ Do not add `before_model_callback` hooks
- ❌ Do not implement function-call/response pairing logic
- ❌ Do not implement state-machine resumption
- ❌ Do not modify ADK internals
- ❌ Do not write code to "catch up" compaction on resumed sessions
- ❌ Do not add cross-session memory recall
- ❌ Do not implement per-agent cap configuration (one global cap is enough)

If any of these prove necessary in production, they will be scoped as separate work items after Phase 3.

---

## 5. Definition of Done

This work is complete when:

1. ✅ All 10 tasks above are checked off
2. ✅ All unit tests pass (`pytest tests/test_resumable_session.py`)
3. ✅ All integration tests pass against real Postgres
4. ✅ The application has been deployed through Phase 3
5. ✅ The runbook is committed and reviewed
6. ✅ The `resumable_session.cap_hit` metric is visible in the team's monitoring dashboard
7. ✅ A post-rollout review document is written summarizing: actual cap value chosen, observed cap-hit rate, before/after first-turn latency, before/after first-turn token cost

---

## 6. Quick Reference — Final File Tree

```
src/
  resumable/
    __init__.py
    session_service.py
    observability.py
tests/
  __init__.py
  test_resumable_session.py
  test_integration.py
docs/
  resumable-session-service.md
pyproject.toml             # modified: pin google-adk==1.21.*
<app_bootstrap>.py         # modified: use ResumableDatabaseSessionService
```

---

## 7. Working Notes for the Implementer

**Pitfalls to avoid**:

- **Do not mutate the caller's `GetSessionConfig` object.** Always create a fresh one when we apply the cap. Mutating shared state is a foot-gun.
- **Do not assume `GetSessionConfig.num_recent_events` always exists** as a settable attribute — verify on your installed ADK 1.21.x. If the dataclass is frozen, construct a new one instead of setting the attribute.
- **Do not test against `InMemorySessionService`** for the cap-hit metric tests. Use real Postgres in integration tests, mocks in unit tests.
- **Do not log at WARN or ERROR for cap hits.** A cap hit is expected behavior, not an error. Use INFO.
- **Do not call `super().__init__()` after setting subclass attributes.** Always call super first.

**Verification commands** (run these between tasks):

```bash
# After Task 2:
python -c "from resumable.observability import record_cap_hit; record_cap_hit(app_name='x')"

# After Task 3:
python -c "from resumable.session_service import ResumableDatabaseSessionService; ResumableDatabaseSessionService.__init__"

# After Task 4:
python -c "from resumable import ResumableDatabaseSessionService, UNBOUNDED; assert UNBOUNDED is not None"

# After Task 5:
pytest tests/test_resumable_session.py -v --cov=resumable.session_service --cov-report=term-missing

# After Task 6:
pytest tests/test_integration.py -v

# After Task 7:
# Run the existing application test suite — must still pass.

# After Task 10 Phase 1:
# Check metrics dashboard. cap_hit counter should report 0.
```

---

## 8. Questions to Confirm Before Starting

If any of these are unclear, surface to the human reviewer before implementation:

1. What is the exact path to the application's bootstrap file (where `DatabaseSessionService` is currently instantiated)?
2. Is there an existing OpenTelemetry exporter configured? If not, the metric will be recorded but not exported.
3. What is the current `compaction_interval` in the application's `EventsCompactionConfig`? (Affects cap tuning.)
4. Are there any non-runner consumers of `get_session()` we need to migrate to `UNBOUNDED`?
5. Is `testcontainers-python` acceptable for integration tests, or is there a preferred Postgres test harness?

---

**End of Plan.** Begin with Task 1.
