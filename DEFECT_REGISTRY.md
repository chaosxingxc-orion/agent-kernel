# Defect Registry — agent-kernel

> Last updated: 2026-04-03 (Round 11 — DEF-001~027 Fixed)  
> Scope: Full codebase — kernel, persistence, substrate, runtime, adapters layers (all 60 source files)  
> Methodology: First-hand code reading with line-level evidence + parallel agent scan of remaining 38 files. Only confirmed defects included.

---

## Summary

| ID | Severity | Category | File | Status |
|----|----------|----------|------|--------|
| DEF-001 | HIGH | Wrong attribute access — silent observability blackout | `kernel/turn_engine.py:662,675` | **Fixed** |
| DEF-002 | HIGH | Non-atomic read-check-update in DedupeStore state transitions | `kernel/persistence/sqlite_dedupe_store.py:117-127,143-153,164-174` | **Fixed** |
| DEF-003 | HIGH | `_update_state` never checks `rowcount` — silent lost-update | `kernel/persistence/sqlite_dedupe_store.py:246-261` | **Fixed** |
| DEF-004 | MEDIUM | Unsafe default `acknowledged=True` masks executor silent failures | `kernel/turn_engine.py:724` | **Fixed** |
| DEF-005 | MEDIUM | `configure_run_actor_dependencies` is not thread-safe under concurrent KernelRuntime init | `substrate/temporal/run_actor_workflow.py:155-156` | **Fixed** |
| DEF-006 | HIGH | `ValueError` from `assert_snapshot_compatible` escapes `_phase_snapshot` except block | `kernel/turn_engine.py:547,549` | **Fixed** |
| DEF-007 | HIGH | `_normalize_host_kind` silently drops `"cli_process"` and `"in_process_python"` | `kernel/turn_engine.py:1278` | **Fixed** |
| DEF-008 | HIGH | `asyncio.run()` in sync consistency checker silently fails in async context | `kernel/persistence/consistency.py:122` | **Fixed** |
| DEF-009 | MEDIUM | `record_failure` dereferences `fetchone()` result without None guard | `kernel/persistence/sqlite_circuit_breaker_store.py:97` | **Fixed** |
| DEF-010 | MEDIUM | `SchemaApplicator` accepts duplicate migration version numbers without validation | `kernel/persistence/migrations.py:70-73` | **Fixed** |
| DEF-011 | HIGH | `_phase_noop_or_reasoning` — reasoning loop exceptions bypass FSM, no `TurnResult` produced | `kernel/turn_engine.py:502` | **Fixed** |
| DEF-012 | HIGH | `event_log_health_check` always returns `OK` — inner `suppress` makes outer `except` dead code | `runtime/health.py:200,206` | **Fixed** |
| DEF-013 | MEDIUM | `worker.run()` finally block calls `configure_run_actor_dependencies(None)` instead of `clear_run_actor_dependencies(token)` | `substrate/temporal/worker.py:86` | **Fixed** |
| DEF-014 | HIGH | `_SharedConnectionEventLog.append_action_commit` non-atomic on autocommit connection — partial event persists on failure | `kernel/persistence/sqlite_colocated_bundle.py:59` | **Fixed** |
| DEF-015 | HIGH | `_SharedConnectionDedupeStore` mark_* methods: TOCTOU — lock released between `_require()` read and `_update()` write | `kernel/persistence/sqlite_colocated_bundle.py:217-261` | **Fixed** |
| DEF-016 | MEDIUM | `_SharedConnectionDedupeStore._update()` no `rowcount` check — silent lost-update on deleted key | `kernel/persistence/sqlite_colocated_bundle.py:306-314` | **Fixed** |
| DEF-007b | HIGH | `_parse_idempotency_envelope` also calls broken `_normalize_host_kind` — admission envelopes with `cli_process`/`in_process_python` silently discarded | `kernel/turn_engine.py:904-906` | **Fixed** |
| DEF-017 | HIGH | `_execute_group` timeout discards partial results — completed branches before timeout misclassified as failures, triggering spurious rollback signals | `kernel/plan_executor.py:244-253` | **Fixed** |
| DEF-018 | MEDIUM | R6a crash-replay dedupe skip in `_monitored_run` is permanently dead code — PlanExecutor never writes to `self._dedupe_store`, so `existing` is always `None` | `kernel/plan_executor.py:218-228` | **Fixed** |
| DEF-019 | HIGH | Two circuit breaker bugs in `PlannedRecoveryGateService.decide()`: (A) `effect_class` variable shadowed in compensation block — failures silently not recorded; (B) timer reset on every rejected request blocks half-open recovery indefinitely | `kernel/recovery/gate.py:165,193,237,167,301` | **Fixed** |
| DEF-020 | HIGH | `CompensationRegistry.execute()` silently swallows `compensate()` failure, leaves dedupe slot stuck in "dispatched", reports `True` (success) to caller — compensation permanently blocked for that `(effect_class, action_id)` pair | `kernel/recovery/compensation_registry.py:248-255` | **Fixed** |
| DEF-021 | CRITICAL | `_SharedConnectionDedupeStore` mark_* methods hold Python lock only for individual SQL calls, not full BEGIN→COMMIT span — concurrent callers get `OperationalError` instead of `DedupeStoreStateError` | `kernel/persistence/sqlite_colocated_bundle.py:224-289` | **Fixed** |
| DEF-022 | HIGH | `averify_event_dedupe_consistency` silently swallows `event_log.load()` exceptions — returns "no violations" on I/O failure, producing false-negative consistency reports | `kernel/persistence/consistency.py:220-223` | **Fixed** |
| DEF-023 | HIGH | `SQLiteCircuitBreakerStore.record_failure()` UPSERT + separate SELECT not in one transaction, no threading lock — returned failure count may reflect another thread's write | `kernel/persistence/sqlite_circuit_breaker_store.py:81-102` | **Fixed** |
| DEF-024 | MEDIUM | `SQLiteRecoveryOutcomeStore.write_outcome()` and `SQLiteTurnIntentLog.write_intent()` have no ROLLBACK on exception — a failed `commit()` leaves the connection in an open transaction, blocking all subsequent writes | `kernel/persistence/sqlite_recovery_outcome_store.py:25-49`, `kernel/persistence/sqlite_turn_intent_log.py:24-61` | **Fixed** |
| DEF-025 | LOW | `_collect_dedupe_keys` uses unescaped `run_id` in SQLite LIKE — `%` or `_` in run_id causes wildcard expansion, returning wrong dedupe keys in consistency check | `kernel/persistence/consistency.py:311` | **Fixed** |
| DEF-026 | MEDIUM | `InProcessPythonScriptRuntime._run_sync` creates `stderr_buf` but never redirects `sys.stderr` to it — `ScriptResult.stderr` is always empty for in-process scripts | `kernel/cognitive/script_runtime.py:168-184` | **Fixed** |
| DEF-027 | LOW | `LocalProcessScriptRuntime.execute_script` calls `proc.kill()` on timeout without `await proc.wait()` — zombie processes accumulate under repeated timeouts | `kernel/cognitive/script_runtime.py:273-274` | **Fixed** |

---

## DEF-001 — Wrong attribute on `TurnInput` silently kills observability

**Severity**: HIGH  
**Category**: Wrong attribute access / silent failure  
**File**: `agent_kernel/kernel/turn_engine.py:662,675`

### Evidence

```python
# TurnInput (line 87-102) — only has run_id directly:
@dataclass(frozen=True, slots=True)
class TurnInput:
    run_id: str
    through_offset: int
    based_on_offset: int
    trigger_type: TurnTriggerType
    history: list[Any] = field(default_factory=list)
    # NO execution_context attribute

# _phase_dedupe (line 659-665) — wrong attribute path:
if self._observability_hook is not None:
    with contextlib.suppress(Exception):
        self._observability_hook.on_dedupe_hit(
            run_id=ctx.input_value.execution_context.run_id,  # BUG line 662
            ...
        )

# Same bug at line 673-678:
self._observability_hook.on_dedupe_hit(
    run_id=ctx.input_value.execution_context.run_id,  # BUG line 675
    ...
)
```

`ctx.input_value` is a `TurnInput`. It has `run_id` directly at the top level, not via `.execution_context.run_id`. The attribute access raises `AttributeError` which is swallowed by `contextlib.suppress(Exception)`.

Note: `ctx.execution_context` (a `TurnPhaseContext` field) IS set later in `_phase_execute`, but `_phase_dedupe` runs before that. Even using `ctx.execution_context` would be wrong here since it is `None` during the dedupe phase.

### Impact
`on_dedupe_hit` is **never called** in any dedupe scenario. All deduplication observability is silently dead. This is invisible in tests if hooks are not asserted.

### Fix
```python
# line 662 and 675:
run_id=ctx.input_value.run_id,
```

---

## DEF-002 — Non-atomic read-check-update in DedupeStore state transitions

**Severity**: HIGH  
**Category**: TOCTOU race condition / state machine violation  
**File**: `agent_kernel/kernel/persistence/sqlite_dedupe_store.py:117-127, 143-153, 164-174`

### Evidence

```python
# sqlite_dedupe_store.py — isolation_level=None (autocommit) set at line 31:
self._conn = sqlite3.connect(self._database_path, isolation_level=None)

# mark_dispatched (line 103-127) — three separate autocommit SQL ops:
def mark_dispatched(self, dispatch_idempotency_key, ...):
    record = self._get_required_record(...)    # SELECT — autocommit op #1
    if record.state not in ("reserved", "dispatched"):
        raise DedupeStoreStateError(...)
    self._update_state(..., state="dispatched")  # UPDATE — autocommit op #2
```

`reserve()` correctly uses `BEGIN IMMEDIATE` ... `COMMIT` (line 65-97). The three state-advance methods (`mark_dispatched`, `mark_acknowledged`, `mark_unknown_effect`) do NOT. Between the SELECT in `_get_required_record` and the UPDATE in `_update_state`, another process can change the row's state in SQLite. The state guard is checked against a stale snapshot.

### Concrete race scenario
1. Process A reads record: state="dispatched"
2. Process B reads record: state="dispatched"
3. Process A calls `mark_acknowledged` → UPDATE, record now "acknowledged"
4. Process B calls `mark_acknowledged` → guard passes (stale read), UPDATE to "acknowledged" again — no error raised but duplicate ack is recorded

A more dangerous variant:
1. Process A: `_get_required_record` → state="reserved"
2. Reconciler: `mark_dispatched` + `mark_unknown_effect` → state="unknown_effect"
3. Process A: guard passes (stale "reserved"), `_update_state` → silently overwrites "unknown_effect" → **back to "dispatched"**, corrupting the state machine

### Impact
State machine invariants can be violated in multi-process SQLite deployments. Idempotency guarantees are undermined.

### Fix
Wrap each of `mark_dispatched`, `mark_acknowledged`, `mark_unknown_effect` in a `BEGIN IMMEDIATE` ... `COMMIT` transaction (same pattern as `reserve()`), re-reading state inside the transaction.

---

## DEF-003 — `_update_state` never checks `rowcount` — silent lost-update

**Severity**: HIGH  
**Category**: Silent data loss / lost-update  
**File**: `agent_kernel/kernel/persistence/sqlite_dedupe_store.py:246-261`

### Evidence

```python
def _update_state(self, dispatch_idempotency_key, state, peer_operation_id, external_ack_ref):
    cursor = self._conn.cursor()
    cursor.execute(
        "UPDATE dedupe_store SET state=?, ... WHERE dispatch_idempotency_key=?",
        (state, peer_operation_id, external_ack_ref, dispatch_idempotency_key),
    )
    # isolation_level=None (autocommit) — each statement commits automatically.
    # cursor.rowcount is NEVER checked.
```

If the row was deleted between the `_get_required_record` read and this UPDATE (e.g., by a concurrent cleanup job, a test teardown, or admin tooling), the UPDATE silently affects 0 rows. The caller receives no error; the state change is lost.

### Impact
A state transition (e.g., `dispatched → acknowledged`) can silently no-op. The dedupe record vanishes while the caller believes the key is now "acknowledged". On retry, the next `reserve()` succeeds (key missing), allowing a duplicate dispatch.

### Fix
After `cursor.execute(...)`, assert `cursor.rowcount == 1`:
```python
if cursor.rowcount != 1:
    raise DedupeStoreStateError(
        f"Lost-update: key {dispatch_idempotency_key!r} not found during state transition."
    )
```
This fix also partially mitigates DEF-002 by making races visible rather than silent.

---

## DEF-004 — `acknowledged=True` default masks executor silent failures

**Severity**: MEDIUM  
**Category**: Unsafe default / silent success misreport  
**File**: `agent_kernel/kernel/turn_engine.py:724`

### Evidence

```python
# _phase_execute (line 724):
acknowledged = bool(ctx.execute_result.get("acknowledged", True))
```

`ExecutorPort.execute()` contract (line 204-230) returns `dict[str, Any]`. No field is declared mandatory. If an executor implementation returns `{}` or omits the `"acknowledged"` key (e.g., due to a bug, an incomplete implementation, or an exception absorbed internally), the default `True` makes the turn engine treat the dispatch as fully acknowledged.

### Impact
- DedupeStore is advanced to "acknowledged"
- `dispatch_acknowledged` event is emitted
- Downstream considers the action committed
- The actual executor may have done nothing

This is particularly dangerous for custom executor implementations during development, where omitting a return key is a common mistake that produces no visible error.

### Fix
Either (a) require the key explicitly and raise on absence:
```python
if "acknowledged" not in ctx.execute_result:
    raise ExecutorContractError("executor must return 'acknowledged' key")
acknowledged = bool(ctx.execute_result["acknowledged"])
```
Or (b) default to `False` (fail-safe):
```python
acknowledged = bool(ctx.execute_result.get("acknowledged", False))
```

---

## DEF-005 — `configure_run_actor_dependencies` is not thread-safe

**Severity**: MEDIUM  
**Category**: Concurrency / race condition  
**File**: `agent_kernel/substrate/temporal/run_actor_workflow.py:132-157`

### Evidence

```python
_RUN_ACTOR_CONFIG: ContextVar[RunActorDependencyBundle | None] = ContextVar(...)
_RUN_ACTOR_CONFIG_FALLBACK: dict[str, RunActorDependencyBundle | None] = {"dependencies": None}

def configure_run_actor_dependencies(dependencies):
    _RUN_ACTOR_CONFIG.set(dependencies)
    _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] = dependencies  # line 156 — not atomic with line 155
    return dependencies

def clear_run_actor_dependencies(token):
    # reads and compares _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] vs token
    if _RUN_ACTOR_CONFIG_FALLBACK.get("dependencies") is token:
        _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] = None
```

`configure_run_actor_dependencies` performs two independent writes (ContextVar + dict). In a Temporal worker that starts multiple `KernelRuntime` instances concurrently (each calling `configure_run_actor_dependencies`), there is a window where:
- Thread A: ContextVar set to bundle-A
- Thread B: ContextVar set to bundle-B
- Thread B: fallback dict set to bundle-B
- Thread A: fallback dict set to bundle-A  ← overwrites B's fallback

Any workflow on Thread B that falls back to the dict now sees bundle-A's services (wrong admission service, wrong executor).

### Impact
In multi-runtime Temporal workers, a run can execute with another run's dependency bundle — wrong executor, wrong admission service. This is a hard-to-reproduce intermittent bug that only manifests under concurrent startup.

### Fix
Use a per-run-id key in `_RUN_ACTOR_CONFIG_FALLBACK` rather than a single `"dependencies"` slot, or replace the dict with a `threading.local()` to give each thread its own slot.

---

## DEF-006 — `ValueError` from `assert_snapshot_compatible` escapes `_phase_snapshot`

**Severity**: HIGH  
**Category**: Missing exception type in except clause  
**File**: `agent_kernel/kernel/turn_engine.py:547,549`

### Evidence

```python
# capability_snapshot.py:302-322 — raises ValueError, not CapabilitySnapshotBuildError:
def assert_snapshot_compatible(snapshot: CapabilitySnapshot) -> None:
    if snapshot.snapshot_schema_version != _CURRENT_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"CapabilitySnapshot schema_version={snapshot.snapshot_schema_version!r} ..."
        )

# turn_engine.py:539-560 — except only catches CapabilitySnapshotBuildError:
try:
    snapshot_input = _resolve_snapshot_input(...)
    ctx.snapshot = self._snapshot_builder.build(snapshot_input)
    assert_snapshot_compatible(ctx.snapshot)       # line 547 — raises ValueError
    ctx.emitted_events.append(TurnStateEvent(state="snapshot_built"))
except CapabilitySnapshotBuildError:               # line 549 — does NOT catch ValueError
    ctx.result = TurnResult(state="completed_noop", ...)
```

`assert_snapshot_compatible` is documented as raising `ValueError` (its docstring says `Raises: ValueError`). The except clause catches only `CapabilitySnapshotBuildError`. A schema version mismatch (e.g., a snapshot serialized with an older kernel version replayed against a newer binary) causes `ValueError` to escape the try block, leaving `ctx.result` unset. The FSM loop's safety net at line 469 then raises `RuntimeError("TurnEngine phases completed without setting ctx.result.")`.

### Trigger
Any workflow that replays a persisted `CapabilitySnapshot` built by a previous kernel version after a schema version bump (`_CURRENT_SNAPSHOT_SCHEMA_VERSION` changed).

### Impact
`run_turn()` raises `RuntimeError` instead of returning a `TurnResult`. In Temporal, this causes the workflow step to fail and retry. The run stalls permanently if the schema mismatch is persistent (e.g., rolling deployment where old and new binaries coexist).

### Fix
```python
except (CapabilitySnapshotBuildError, ValueError):
```

---

## DEF-007 — `_normalize_host_kind` silently drops `"cli_process"` and `"in_process_python"`

**Severity**: HIGH  
**Category**: Incomplete literal guard — silent routing failure  
**File**: `agent_kernel/kernel/turn_engine.py:1278`

### Evidence

```python
# dedupe_store.py:14-20 — full HostKind definition:
HostKind = Literal[
    "local_process",
    "local_cli",
    "cli_process",          # ← valid value
    "in_process_python",    # ← valid value
    "remote_service",
]

# turn_engine.py:1266-1280:
def _normalize_host_kind(value: Any) -> HostKind | None:
    if not isinstance(value, str):
        return None
    normalized_value = value.strip().lower()
    if normalized_value in ("local_cli", "local_process", "remote_service"):  # ← missing 2 values
        return normalized_value
    return None
```

`_normalize_host_kind` is the sole path by which an action's `host_kind` field is resolved from the action's `dispatch` payload. `"cli_process"` and `"in_process_python"` are valid `HostKind` literals but are absent from the membership test. For any action tagged with either value, the function returns `None`, and the callers fall through to the next extraction key or use the default (`"local_process"`).

### Trigger
Any action whose `dispatch.host_kind` payload field is `"cli_process"` or `"in_process_python"`.

### Impact
- The action is dispatched to the wrong host kind (default rather than declared).
- Idempotency envelope `host_kind` field is wrong — dedupe keys calculated by different processes that use different defaults will diverge.
- The error is completely silent: no log, no exception, no `dispatch_blocked`.

### Fix
```python
if normalized_value in (
    "local_cli", "local_process", "cli_process", "in_process_python", "remote_service"
):
```

---

## DEF-008 — `asyncio.run()` in sync consistency checker silently swallows all violations

**Severity**: HIGH  
**Category**: Async/sync mismatch — silent safety-check bypass  
**File**: `agent_kernel/kernel/persistence/consistency.py:118-124`

### Evidence

```python
# consistency.py:118-124 — sync fallback path for async event logs:
else:
    # Try the async load() via asyncio.run()
    try:
        import asyncio
        events = asyncio.run(event_log.load(run_id))   # line 122
    except Exception:                                   # line 123 — swallows RuntimeError
        pass
```

`asyncio.run()` raises `RuntimeError: This event loop is already running` when called from within an existing asyncio event loop (including Temporal workflow and activity contexts, pytest-asyncio test cases, and any `async def` caller). That `RuntimeError` is caught by `except Exception: pass`. The result: `events` stays `[]`, `event_idempotency_keys` is empty, and `_collect_dedupe_keys` is compared against an empty set — **every real orphaned dedupe key is missed**. The consistency report returns clean (`violations_found=0`) when it should report violations.

### Trigger
`verify_event_dedupe_consistency()` called from any async context where `SQLiteKernelRuntimeEventLog` (or any async event log) is used. This is the expected production usage in Temporal activities.

### Impact
The `DispatchOutboxReconciler` receives a clean report and performs no repairs. Orphaned "reserved" or "dispatched" dedupe keys (from crashed processes) are never transitioned to "unknown_effect" and never surfaced for recovery. The recovery layer is effectively disabled in async contexts.

### Fix
Either (a) make `verify_event_dedupe_consistency` async (with an `await event_log.load(...)` call), or (b) provide a sync accessor on the event log, or (c) use `asyncio.get_event_loop().run_until_complete()` with a guard, or (d) accept the limitation and document that `reconcile_sync` only works with `InMemoryKernelRuntimeEventLog`.

---

## DEF-009 — `record_failure` dereferences `fetchone()` result without None guard

**Severity**: MEDIUM  
**Category**: Unguarded None dereference  
**File**: `agent_kernel/kernel/persistence/sqlite_circuit_breaker_store.py:93-97`

### Evidence

```python
def record_failure(self, effect_class: str) -> int:
    now = time.time()
    self._conn.execute(
        "INSERT INTO circuit_breaker_state (...) VALUES (?, 1, ?)"
        "ON CONFLICT(effect_class) DO UPDATE SET failure_count = failure_count + 1, ...",
        (effect_class, now),
    )
    self._conn.commit()                       # line 92 — committed
    row = self._conn.execute(                 # line 93 — second SELECT
        "SELECT failure_count FROM circuit_breaker_state WHERE effect_class = ?",
        (effect_class,),
    ).fetchone()
    return int(row[0])                        # line 97 — row not checked for None
```

Between the `COMMIT` (line 92) and the subsequent `SELECT` (line 93), another thread or process could delete the row (e.g., `reset()` called concurrently). `fetchone()` would return `None`, and `int(row[0])` raises `TypeError: 'NoneType' object is not subscriptable`. The circuit breaker's failure-recording path crashes, masking the original failure that triggered `record_failure`.

### Trigger
Concurrent `reset(effect_class)` call between the `COMMIT` and the `SELECT` in `record_failure`.

### Impact
`TypeError` propagates from the circuit breaker, replacing the original executor exception with a different, confusing error. Circuit breaker state for the affected `effect_class` is undefined until the next call.

### Fix
```python
return int(row[0]) if row is not None else 1
```

---

## DEF-010 — `SchemaApplicator` accepts duplicate migration version numbers without validation

**Severity**: MEDIUM  
**Category**: Missing input validation — obscure failure at startup  
**File**: `agent_kernel/kernel/persistence/migrations.py:58-73`

### Evidence

```python
def __init__(self, connection, extra_migrations=None):
    self._migrations = list(KERNEL_MIGRATIONS)
    if extra_migrations:
        for m in extra_migrations:
            self._migrations.append(m)     # line 72 — no uniqueness check
        self._migrations.sort(key=lambda m: m.version)

def _apply(self, migration):
    with self._connection:
        self._connection.executescript(migration.sql)
        self._connection.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
            (migration.version, migration.description),   # line 120 — PRIMARY KEY constraint
        )
```

The docstring states "Must have unique version numbers that do not overlap with built-in versions" but this is not enforced. If a caller passes `extra_migrations=[Migration(version=1, ...)]` (same as a built-in), `apply_all()` filters out already-applied versions correctly for the first pass. But if the same `SchemaApplicator` instance is used across tests or the list is built incorrectly, two migrations with the same version number can both appear in `pending` and the second `_apply` call fails with `sqlite3.IntegrityError: UNIQUE constraint failed: schema_migrations.version`. This exception propagates uncaught, leaving the schema partially applied.

### Trigger
Any caller passing `extra_migrations` with a version number that duplicates either a built-in migration or another `extra_migration`.

### Impact
`apply_all()` raises `IntegrityError` mid-migration. The database is left in a partially migrated state. Subsequent startup attempts will either re-apply the failed migration or skip it (depending on which tables were created before the error), leading to inconsistent schemas.

### Fix
Add a uniqueness check in `__init__`:
```python
seen_versions = {m.version for m in self._migrations}
for m in extra_migrations:
    if m.version in seen_versions:
        raise ValueError(f"Duplicate migration version {m.version} in extra_migrations.")
    seen_versions.add(m.version)
    self._migrations.append(m)
```

---

## DEF-011 — Reasoning loop exceptions propagate uncaught, bypassing FSM and producing no `TurnResult`

**Severity**: HIGH  
**Category**: Missing exception handling — uncontrolled failure mode  
**File**: `agent_kernel/kernel/turn_engine.py:502-531`

### Evidence

```python
async def _phase_noop_or_reasoning(self, ctx: TurnPhaseContext) -> None:
    # ...
    ctx.emitted_events.append(TurnStateEvent(state="reasoning"))
    reasoning_result = await self._reasoning_loop.run_once(   # line 502 — no try/except
        run_id=ctx.input_value.run_id,
        snapshot=self._snapshot_builder.build(...),
        history=list(ctx.input_value.history),
        inference_config=inference_config,
        idempotency_key=reasoning_idempotency_key,
    )
    # lines 516-531: set ctx.result only on success paths
```

`reasoning_loop.run_once()` can raise for any number of reasons: LLM API timeout, network error, output parsing failure, token budget exhaustion, etc. There is no try/except around line 502. If an exception is raised:

1. `ctx.result` is never set.
2. The exception propagates up through the FSM loop at `await phase_fn(ctx)` (line 456).
3. The FSM loop's safety net (`raise RuntimeError(...)` at line 469) is **never reached** — the exception escapes `run_turn()` entirely.
4. The caller of `run_turn()` (typically `RunActorWorkflow`) receives an unhandled exception rather than a `TurnResult`.

In Temporal, this causes the workflow step to fail and be retried. For transient errors (network timeout), this is harmless. For persistent errors (output parsing always failing for a given model), the workflow retries indefinitely, consuming resources and blocking the run.

### Trigger
Any transient or permanent error in `reasoning_loop.run_once()` when `self._reasoning_loop is not None`.

### Impact
- Temporal retries the entire workflow step on every reasoning failure.
- No `TurnResult` is produced, so no event is appended to the event log.
- The run can be stuck in an infinite retry loop without any visible "noop" or "recovery_pending" signal.

### Fix
Wrap line 502 in a try/except:
```python
try:
    reasoning_result = await self._reasoning_loop.run_once(...)
except Exception as exc:
    ctx.emitted_events.append(TurnStateEvent(state="completed_noop", reason=str(exc)))
    ctx.result = TurnResult(state="completed_noop", outcome_kind="noop", ...)
    return
```

---

---

## DEF-012 — `event_log_health_check` always returns `OK` — outer `except` is dead code

**Severity**: HIGH  
**Category**: Health probe false positive — K8s readiness gate unreliable  
**File**: `agent_kernel/runtime/health.py:197-209`

### Evidence

```python
def _check() -> tuple[HealthStatus, str]:
    try:
        count = -1
        with contextlib.suppress(Exception):      # ← absorbs ALL exceptions inside
            if hasattr(event_log, "list_events"):
                count = len(event_log.list_events())
            elif hasattr(event_log, "events"):
                count = len(event_log.events)
        return HealthStatus.OK, f"EventLog reachable ({count} events)"  # ← always reached
    except Exception as exc:                      # ← DEAD CODE: nothing can reach here
        return HealthStatus.UNHEALTHY, f"EventLog unreachable: {exc}"
```

`contextlib.suppress(Exception)` swallows every exception raised by `list_events()` or `len()`. The `return HealthStatus.OK` line is always reached regardless of whether the event log is accessible. The outer `except Exception` block can never execute — there is no code path between the `suppress` block and the `return` that can raise.

When the event log is broken (connection closed, file missing, internal error), `count` stays `-1`, and `_check()` returns:
```
(HealthStatus.OK, "EventLog reachable (-1 events)")
```

### Impact
- The `event_log_health_check` **always** reports `HealthStatus.OK`.
- `KernelHealthProbe.readiness()` never reports `UNHEALTHY` or `DEGRADED` for the event log.
- K8s readiness gates and operator dashboards see the event log as healthy when it is down.
- Production traffic is routed to a pod with a broken event log.

### Contrast
`sqlite_dedupe_store_health_check` at line 159 is correctly implemented without `suppress`.

### Fix
Remove `contextlib.suppress` and let exceptions propagate to the outer `except`:
```python
def _check() -> tuple[HealthStatus, str]:
    try:
        count = -1
        if hasattr(event_log, "list_events"):
            count = len(event_log.list_events())
        elif hasattr(event_log, "events"):
            count = len(event_log.events)
        return HealthStatus.OK, f"EventLog reachable ({count} events)"
    except Exception as exc:
        return HealthStatus.UNHEALTHY, f"EventLog unreachable: {exc}"
```

---

## DEF-013 — `worker.run()` finally uses `configure_run_actor_dependencies(None)` instead of `clear_run_actor_dependencies(token)`

**Severity**: MEDIUM  
**Category**: Unsafe teardown — process-wide dependency wipe  
**File**: `agent_kernel/substrate/temporal/worker.py:72,86`

### Evidence

```python
# worker.py:72-86:
configure_run_actor_dependencies(self._dependencies)     # line 72 — registers bundle
try:
    ...
    await worker.run()
finally:
    # Always clear process-local workflow dependency override …
    configure_run_actor_dependencies(None)               # line 86 — unconditional wipe
```

`clear_run_actor_dependencies` exists precisely for safe teardown. Its docstring states:

> **Safe to call concurrently**: if another ``KernelRuntime`` has already registered its own bundle, this call is a no-op and the new runtime's state is not disturbed.

It achieves this via a token identity check:
```python
# run_actor_workflow.py:173-175:
def clear_run_actor_dependencies(token):
    if _RUN_ACTOR_CONFIG_FALLBACK.get("dependencies") is token:   # ← only clears if matches
        _RUN_ACTOR_CONFIG.set(None)
        _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] = None
```

`worker.py` bypasses this safety by calling `configure_run_actor_dependencies(None)` directly, which **unconditionally** overwrites both the `ContextVar` and the shared `_RUN_ACTOR_CONFIG_FALLBACK` dict with `None`.

### Trigger
Two `TemporalKernelWorker` instances run concurrently in the same process (different task queues, graceful rolling restart, test isolation). When Worker A stops, its `finally` block wipes the shared dict, so Worker B's workflows cannot find their dependency bundle and fall back to `None`.

### Impact
- Worker B's `RunActorWorkflow.__init__` receives `None` dependencies at the point it was expecting its own bundle.
- Subsequent workflow executions on Worker B start with `None` services — null event log, null executor, null admission service.
- Failures are silent and manifested as `AttributeError`/`NoneType` errors deep inside workflow execution, not at worker startup.

### Fix
Replace line 86:
```python
# before:
configure_run_actor_dependencies(None)
# after:
clear_run_actor_dependencies(self._dependencies)
```

---

---

## DEF-014 — `_SharedConnectionEventLog.append_action_commit` non-atomic on autocommit connection

**Severity**: HIGH  
**Category**: Missing transaction — partial write on failure  
**File**: `agent_kernel/kernel/persistence/sqlite_colocated_bundle.py:55-63`

### Evidence

```python
# ColocatedSQLiteBundle.__init__ (line 343-345):
self._conn = sqlite3.connect(
    self._database_path,
    isolation_level=None,   # ← autocommit; each statement commits immediately
    check_same_thread=False,
)

# _SharedConnectionEventLog.append_action_commit (line 55-63):
async def append_action_commit(self, commit: ActionCommit) -> str:
    if not commit.events:
        raise ValueError(...)
    with self._lock, self._conn:            # ← self._conn context manager on autocommit
        next_offset = self._next_offset(commit.run_id)
        seq = self._insert_commit_row(commit)          # ← INSERT committed immediately
        self._insert_event_rows(commit.run_id, seq,    # ← each INSERT committed immediately
                                commit.events, next_offset)
    return f"commit-ref-{seq}"
```

For `isolation_level=None` (autocommit), Python's `sqlite3.Connection.__exit__` calls `conn.commit()`, which internally checks `sqlite3_get_autocommit()`. In autocommit mode this returns 1 (no active transaction), so `commit()` is a no-op. **`with self._conn:` does not start or protect a transaction.** Every `execute(...)` is immediately committed as a standalone transaction.

If `_insert_event_rows` raises after `_insert_commit_row` succeeds (e.g., `UNIQUE (stream_run_id, commit_offset)` violation for the second event in a multi-event commit), the commit row is permanently committed but its events are partially missing. The `with self._conn:` `__exit__` calls `rollback()`, which is also a no-op in autocommit mode.

This is the colocated-bundle equivalent of the split-brain crash window that `atomic_dispatch_record` was specifically designed to prevent — but `atomic_dispatch_record` uses explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` correctly, while the individual `append_action_commit` method does not.

### Trigger
Any multi-event `ActionCommit` appended via `event_log.append_action_commit(commit)` where a later event insert fails (constraint violation, disk full, etc.).

### Impact
- `colocated_action_commits` row exists; `colocated_runtime_events` rows are missing.
- Projection replay over the event log produces an incomplete commit history.
- Recovery and consistency checkers see an orphaned commit with no events.

### Fix
Wrap the inserts in an explicit transaction:
```python
with self._lock:
    self._conn.execute("BEGIN IMMEDIATE")
    try:
        next_offset = self._next_offset(commit.run_id)
        seq = self._insert_commit_row(commit)
        self._insert_event_rows(commit.run_id, seq, commit.events, next_offset)
        self._conn.execute("COMMIT")
    except Exception:
        self._conn.execute("ROLLBACK")
        raise
return f"commit-ref-{seq}"
```

---

## DEF-015 — `_SharedConnectionDedupeStore` mark_* methods: TOCTOU between `_require()` read and `_update()` write

**Severity**: HIGH  
**Category**: TOCTOU race condition — same pattern as DEF-002  
**File**: `agent_kernel/kernel/persistence/sqlite_colocated_bundle.py:217-261`

### Evidence

```python
def _require(self, key: str) -> DedupeRecord:
    with self._lock:          # ← acquires lock
        record = self._get(key)
    # lock released here ←
    if record is None:
        raise DedupeStoreStateError(...)
    return record

def mark_dispatched(self, key, peer_operation_id=None):
    record = self._require(key)        # ← lock acquired + released
    if record.state not in ("reserved", "dispatched"):  # ← guard checked outside lock
        raise DedupeStoreStateError(...)
    self._update(key, "dispatched", ...)  # ← lock acquired again for UPDATE
```

`_require()` acquires `self._lock`, reads the record, then releases the lock before returning. Between the lock release and `_update()` re-acquiring it, another thread can change the record's state in SQLite. The state guard at line 222 is evaluated against a stale snapshot. This is identical in structure to DEF-002 in `SQLiteDedupeStore`, which the colocated bundle was intended to supersede.

### Trigger
Two threads concurrently call `mark_dispatched()` on the same key (e.g., two concurrent reconciliation passes).

### Impact
Invalid state transitions can proceed undetected. A record in "unknown_effect" can be re-transitioned to "dispatched" (bypassing the state machine guard), corrupting idempotency guarantees.

### Fix
Use a single-lock `BEGIN IMMEDIATE`/`COMMIT` block inside each `mark_*` method — same fix as DEF-002.

---

## DEF-016 — `_SharedConnectionDedupeStore._update()` no `rowcount` check — silent lost-update

**Severity**: MEDIUM  
**Category**: Silent lost-update — same pattern as DEF-003  
**File**: `agent_kernel/kernel/persistence/sqlite_colocated_bundle.py:299-314`

### Evidence

```python
def _update(self, key, state, peer_operation_id, external_ack_ref):
    with self._lock:
        self._conn.execute(
            "UPDATE colocated_dedupe_store SET state=?, ... WHERE dispatch_idempotency_key=?",
            (state, peer_operation_id, external_ack_ref, key),
        )
        # ← cursor.rowcount never checked
```

If the row was deleted between the `_require()` read and this UPDATE (e.g., by a concurrent cleanup job), the UPDATE affects 0 rows. No error is raised, and the caller believes the state transition succeeded. This is identical in structure to DEF-003 in `SQLiteDedupeStore`.

### Impact
Silent state transition no-op. On the next `reserve()` call, the key is absent, so a new reservation succeeds — enabling a duplicate dispatch that the dedupe system was meant to prevent.

### Fix
After `execute(...)`, check `cursor.rowcount == 1` and raise `DedupeStoreStateError` if not.

---

## DEF-007b — `_parse_idempotency_envelope` silently discards admission envelopes with `cli_process`/`in_process_python` host kinds

**Severity**: HIGH (extension of DEF-007)  
**Category**: Silent discard — incorrect idempotency envelope  
**File**: `agent_kernel/kernel/turn_engine.py:904-906`

### Evidence

```python
# turn_engine.py:903-906:
normalized_host_kind = _normalize_host_kind(host_kind_value)
if normalized_host_kind is None:
    return None                 # ← entire admission envelope silently discarded
```

`_normalize_host_kind` (line 1278) only recognizes 3 of 5 `HostKind` values (DEF-007). When an admission service returns an `idempotency_envelope` dict with `host_kind="cli_process"` or `"in_process_python"`, `_normalize_host_kind` returns `None`, and `_parse_idempotency_envelope` returns `None`.

`_resolve_idempotency_envelope` then falls back to building a synthetic envelope with defaults:
```python
# turn_engine.py:871-878:
return IdempotencyEnvelope(
    dispatch_idempotency_key=turn_identity.dispatch_dedupe_key,
    operation_fingerprint=turn_identity.decision_fingerprint,
    attempt_seq=1,            # ← default attempt_seq, ignoring admission's value
    effect_scope=action.effect_class,
    capability_snapshot_hash=snapshot.snapshot_hash,
    host_kind=host_kind,      # ← this host_kind comes from _resolve_dispatch_policy, not admission
)
```

The `attempt_seq`, `peer_operation_id`, `policy_snapshot_ref`, and `rule_bundle_hash` provided by the admission service are all lost.

### Impact
- A second dispatch attempt from the same admission service (with `attempt_seq=2`) is treated as `attempt_seq=1` — dedupe key collision if the first attempt's dedupe record is still in "reserved".
- Policy snapshot and rule bundle references in the envelope are zeroed, breaking audit trails.
- All of this is completely silent — no log, no exception, no event.

---

## DEF-017 — `_execute_group` timeout discards completed-branch results, triggering spurious rollbacks

**Severity**: HIGH  
**Category**: Asyncio timeout + partial-result loss — incorrect join evaluation  
**File**: `agent_kernel/kernel/plan_executor.py:244-253`

### Evidence

```python
# plan_executor.py:241-253:
if group.timeout_ms is not None:
    timeout_s = group.timeout_ms / 1000.0
    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*coros, return_exceptions=True),
            timeout=timeout_s,
        )
    except TimeoutError:
        # Entire group timed out — map every action to BranchFailure.
        raw_results = [
            TimeoutError(f"Group timeout after {group.timeout_ms}ms")
            for _ in group.actions
        ]
```

When `asyncio.wait_for` times out, it cancels the inner `asyncio.gather` task. Python's `asyncio.gather` does NOT surface partial results when cancelled — even coroutines that had already completed before the timeout have their results discarded. The code then replaces ALL branch results with `TimeoutError` regardless of individual completion status.

### Trigger

Any parallel group with `timeout_ms` set where at least one branch completes before the timeout fires (e.g., 3 of 5 branches finish in 8 seconds, the group times out at 10 seconds).

### Impact

1. **Incorrect join evaluation**: For strategy `"any"` or `"n_of_m"`, `join_satisfied` is computed as `False` even though enough branches succeeded — halting plans that should have continued.
2. **Spurious rollback signals** (plan_executor.py line 328-336): `on_branch_rollback_triggered` fires for branches that **already succeeded and were persisted to the event log and dedupe store**. The event log says "acknowledged"; the plan says "rollback needed" — state split.
3. The kernel's at-most-once dispatch guarantee is undermined: a successfully committed branch may be retried on the next plan replay because PlanExecutor's join accounting says it failed.

### Fix

Replace `asyncio.wait_for(asyncio.gather(...))` with `asyncio.wait` on a pre-created task set, which allows collecting partial results before cancelling remaining tasks on timeout:

```python
tasks = [asyncio.create_task(_monitored_run(a)) for a in group.actions]
done, pending = await asyncio.wait(tasks, timeout=timeout_s)
for t in pending:
    t.cancel()
await asyncio.gather(*pending, return_exceptions=True)  # drain cancellations
raw_results = [
    t.result() if not t.cancelled() and t.exception() is None
    else (t.exception() if not t.cancelled() else TimeoutError(f"Group timeout after {group.timeout_ms}ms"))
    for t in tasks
]
```

---

## DEF-018 — R6a crash-replay dedupe skip in `_monitored_run` is permanently dead code

**Severity**: MEDIUM  
**Category**: Unreachable code — crash-replay deduplication silently non-functional  
**File**: `agent_kernel/kernel/plan_executor.py:218-228`

### Evidence

```python
# plan_executor.py:217-228:
async def _monitored_run(action: Action) -> TurnResult:
    # R6a: skip already-acknowledged branches on crash-replay.
    if self._dedupe_store is not None:
        branch_key = f"{group.group_idempotency_key}:{action.action_id}"
        existing = self._dedupe_store.get(branch_key)
        if existing is not None and existing.state == "acknowledged":
            return TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                ...
            )
```

`branch_key` is formatted as `f"{group.group_idempotency_key}:{action.action_id}"`. PlanExecutor **never writes** to `self._dedupe_store` using this key — there is no `reserve(branch_key, ...)`, `mark_dispatched(branch_key)`, or `mark_acknowledged(branch_key)` call anywhere in `plan_executor.py`.

TurnEngine writes to the shared dedupe store with a completely different key format:
```
f"{run_id}:{action_id}:{based_on_offset}"
```

Unless `group.group_idempotency_key` happens to equal `f"{run_id}:{based_on_offset}"` (which no production code enforces), `self._dedupe_store.get(branch_key)` will always return `None`. The early-return on line 222 can never trigger.

### Trigger

Any crash-replay scenario where a parallel group is replayed after a worker restart. The R6a skip was designed to prevent re-executing already-acknowledged branches, but the key mismatch means all branches are re-executed unconditionally.

### Impact

- Crash-replay idempotency for parallel branches is **silently broken**. The comment `# R6a: skip already-acknowledged branches on crash-replay` documents an invariant that is not enforced.
- At-most-once dispatch for parallel branches relies solely on TurnEngine's own dedupe logic (which guards the dispatch, but not the entire `_turn_runner` call). If the runner performs any pre-dispatch side effect before TurnEngine's dedupe check, that pre-effect is repeated.
- The `dedupe_store` parameter in `PlanExecutor.__init__` is accepted and stored but provides zero crash-replay protection despite the API contract implying otherwise.

### Fix

PlanExecutor must write its own branch-level acknowledgment to `self._dedupe_store` after a branch completes successfully, using the same `branch_key` format it reads:

```python
# After a successful turn result, record branch ack:
self._dedupe_store.reserve(branch_key, peer_operation_id=branch_key)
self._dedupe_store.mark_dispatched(branch_key)
self._dedupe_store.mark_acknowledged(branch_key)
```

Alternatively, use the TurnEngine's dedupe key format consistently across both writer (TurnEngine) and reader (PlanExecutor), and adjust the lookup accordingly.

---

## DEF-019 — Two circuit breaker bugs in `PlannedRecoveryGateService.decide()`

**Severity**: HIGH  
**Category**: Logic error — circuit breaker permanently stuck OPEN + silent failure non-recording  
**File**: `agent_kernel/kernel/recovery/gate.py:165,167,193,237,301`

### Bug A — `effect_class` variable shadowed, circuit failures silently not recorded

```python
# gate.py:165 — initial assignment:
effect_class = recovery_input.failed_effect_class   # ← correct value

# gate.py:189-193 — reassignment inside compensation block:
if (mode == "static_compensation" and self._compensation_registry is not None):
    effect_class = _extract_effect_class(recovery_input)  # ← OVERWRITES effect_class!
    # _extract_effect_class returns reason_code.split(":")[0] if ":" in reason_code, else None

# gate.py:237 — uses potentially-None or wrong value:
self._record_circuit_failure(effect_class, run_id=recovery_input.run_id)
```

`_extract_effect_class` returns `None` when `reason_code` contains no colon (e.g., `"executor_transient"`, `"heartbeat_timeout"`). In `_record_circuit_failure`, the first line is:
```python
if self._circuit_breaker_policy is None or effect_class is None:
    return  # no-op
```

**Result**: For any `static_compensation` turn with a simple (colon-free) `reason_code`, the circuit failure is silently not recorded. The circuit breaker never opens for those failures, undermining the protection it was added to provide.

### Bug B — Timer reset on every rejected request blocks half-open probe

```python
# gate.py:166-175 — called while circuit is OPEN:
if self._is_circuit_open(effect_class):
    self._record_circuit_failure(effect_class)    # ← resets the cooldown timer!
    ...
    return RecoveryDecision(mode="abort")

# _record_circuit_failure (line 286-306) in-memory path:
self._circuit_last_failure_mono[effect_class] = time.monotonic()   # ← timer reset here
```

`_is_circuit_open` checks `elapsed_ms < self._circuit_breaker_policy.half_open_after_ms`. If every incoming request resets `_circuit_last_failure_mono` to `now`, then `elapsed_ms` is always near zero, and the circuit can NEVER enter half-open state while requests keep coming in.

### Impact

- **Bug A**: Circuit breakers don't trip for `static_compensation` paths under plain reason codes. Repeated compensation-mode failures accumulate without opening the circuit, allowing unbounded retries.
- **Bug B**: Once a circuit opens under any sustained load, it stays open permanently. Half-open probe can only occur after a complete silence period of `half_open_after_ms` milliseconds — which never happens in production traffic.

### Fix

**Bug A**: Use `recovery_input.failed_effect_class` throughout `decide()`. Do not rebind `effect_class` after the initial assignment.

**Bug B**: Do NOT call `_record_circuit_failure` when the circuit is already OPEN and a request is being rejected. The failure was already counted when the circuit opened; rejection-time re-counting resets the timer without reflecting a new real failure.

---

## DEF-020 — `CompensationRegistry.execute()` swallows `compensate()` failure, reports success

**Severity**: HIGH  
**Category**: Silent failure + wrong return value — compensation permanently blocked  
**File**: `agent_kernel/kernel/recovery/compensation_registry.py:239-255`

### Evidence

```python
# compensation_registry.py:237-255:
dedupe_store.mark_dispatched(idempotency_key)

try:
    await entry.compensate(action)                        # ← may raise
    if dedupe_store is not None and idempotency_key is not None:
        dedupe_store.mark_acknowledged(idempotency_key)   # ← only on success
except Exception as exc:
    _comp_logger.error(...)                               # ← logs error...
    # ← NO mark_unknown_effect() call!
    # ← dedupe slot stuck in "dispatched"
return True                                               # ← lies: reports success
```

When `entry.compensate(action)` raises:

1. `mark_dispatched` was already called — the dedupe slot is in `"dispatched"` state.
2. `mark_acknowledged` is skipped.
3. `mark_unknown_effect` is NEVER called — unlike `DedupeAwareScriptRuntime` which correctly calls `mark_unknown_effect` on failure.
4. `execute()` returns `True`, telling the caller "compensation was executed".

### Subsequent recovery rounds

On the next `decide()` call, `reserve()` returns `accepted=False` (duplicate — slot exists in "dispatched"):
```python
if not reservation.accepted:
    ...
    return True   # ← implies "already compensated" — but it was never actually done
```

The compensation is permanently blocked. Every future recovery round silently skips it and returns success. The run is never escalated even though compensation is failing.

### Impact

- A broken compensation handler (network failure, assertion error, missing record) silently marks the recovery as complete.
- The gate issues `RecoveryDecision(mode="static_compensation")` but the compensation never executes.
- `DispatchOutboxReconciler` could surface the stuck "dispatched" key — but `execute()` marks the slot BEFORE calling `compensate`, so the reconciler sees a "dispatched" key with no event log evidence and marks it "unknown_effect". This surfaces the ambiguity but only through the reconciler, not through the gate's own error path.

### Compare with `DedupeAwareScriptRuntime` (correct pattern)

```python
# script_runtime.py:393-396 (correct):
except Exception:
    with contextlib.suppress(Exception):
        self._dedupe_store.mark_unknown_effect(idempotency_key)
    raise
```

### Fix

Add `mark_unknown_effect` in the except block and return `False` (or re-raise):
```python
except Exception as exc:
    _comp_logger.error(...)
    if dedupe_store is not None and idempotency_key is not None:
        with contextlib.suppress(Exception):
            dedupe_store.mark_unknown_effect(idempotency_key)
    return False   # ← report failure, let gate escalate
```

---

## Defects Investigated but NOT Confirmed

| Candidate | Round | Verdict | Reason |
|-----------|-------|---------|--------|
| Temporal `_pending_signals` race | R1 | **Not a bug** | Temporal single-threaded coroutine; `_run_id` set before any await in drain loop |
| `with self._lock, self._connection` deadlock | R1 | **Not a bug** | Valid compound context manager; `with sqlite3.Connection` = implicit transaction |
| `suppress` masking executor exception | R1 | **Not a bug** | `raise` at line 714 re-raises original; `suppress` only guards `mark_unknown_effect` |
| `InMemoryDecisionDeduper` not thread-safe | R1 | **By design** | PoC-only; docstring explicit |
| `capability_snapshot.py` hash missing fields | R2 | **Not a bug** | `budget_ref`, `quota_ref`, `session_mode`, `approval_state` ARE at lines 169-172 of canonical_payload |
| `_phase_admission` missing `return` | R2 | **Not a bug** | Function ends naturally; FSM loop checks `ctx.result is not None` after each phase |
| Heartbeat `age_s` negative from clock skew | R2 | **Not a production hazard** | Negative age → `age_s < timeout_s` is True → reports healthy; safe-direction failure |
| `InMemoryDecisionProjectionService.catch_up` concurrent regression | R3 | **PoC scope** | Stale-write race requires two concurrent asyncio callers; InMemory impl is PoC-only |
| `context_adapter.py` / `checkpoint_adapter.py` / `tool_mcp_adapter.py` / `session_adapter.py` | R6 | **No defects** | Pure mapping layers; no side effects; clean boundary logic |
| `reasoning_loop.py` `run_once` exception handling | R6 | **Not a bug here** | DEF-011 is in `turn_engine.py` line 502 caller, not inside `ReasoningLoop` itself |
| `retry_executor.py` exponential backoff | R6 | **No defects** | Correct: `last_exc` is always set before `raise`, `max_attempts >= 1` enforced |
| `branch_monitor.py` `_parse_ms` / `_now_ms` with tz-naive ISO strings | R6 | **Not a bug** | `datetime.fromisoformat` handles both aware and naive; same source produces same kind |
| `plan_executor.py` `_execute_sequential` — continues past failures | R6 | **By design** | Comment explicitly states "All steps attempted regardless of prior failures" |
| `sqlite_event_log.py` `with self._lock, self._connection` | R6 | **No defect** | Uses default `isolation_level=""` (not autocommit); `with conn:` correctly wraps transaction |
| `sqlite_turn_intent_log.py` / `sqlite_recovery_outcome_store.py` thread safety | R6 | **No defect in asyncio scope** | Both are called from async context only (single event-loop thread); no `check_same_thread=False` needed |
| `failure_evidence.py` / `remote_service_policy.py` | R6 | **No defects** | Pure functional computation; no shared state |
| `event_export.py` `_background_tasks` set prevents GC | R6 | **No defect** | `task.add_done_callback(self._background_tasks.discard)` correctly manages task lifecycle |
| `replay_fidelity.py` `_capture` broad `except Exception: pass` | R6 | **Acceptable** | Verifier is a diagnostic tool; silently returning count=0 on failure is safe for its purpose |
| `reasoning_loop.py` `uuid.uuid4().hex` fallback when `idempotency_key=None` | R9 | **Not a bug** | Both production callers (`turn_engine.py:515`, `gate.py:447`) always pass an explicit deterministic key; `None` path is dead code in production |
| `event_export.py` `EventExportingEventLog._background_tasks` set grows unbounded | R9 | **Not a bug** | `task.add_done_callback(self._background_tasks.discard)` correctly removes tasks on completion — standard fire-and-forget pattern |
| `observability_hooks.py` `OtelObservabilityHook` `token_usage.reasoning_tokens` access | R9 | **Not a bug** | `TokenUsage.reasoning_tokens: int = 0` is always present (default 0) — direct attribute access is safe (reconfirms R8 verdict) |
| `activity_gateway.py` `TemporalSDKActivityGateway` | R9 | **No defects** | Pure dispatch layer; exception propagation is intentionally caller-controlled; no shared mutable state |
| `gateway.py` `query_projection` dict coercion `int(projected_offset)` | R9 | **No defect** | Temporal SDK always returns typed payloads; `int()` coercion with default=0 is acceptable defensive normalization |
| `gateway.py` `cancel_workflow` `except TypeError` for SDK compat | R9 | **No defect** | `TypeError` only arises when `reason=` kwarg is not accepted by older SDK; message says "compatibility note" — intentional compat shim |
| `InProcessPythonScriptRuntime` non-daemon `ThreadPoolExecutor` threads | R9 | **By design** | Per-call executor; `shutdown(wait=False, cancel_futures=True)` on timeout abandons the thread; process exit is handled by asyncio event loop teardown |
| `kernel_facade.py` `_resolve_child_lifecycle_state` `except Exception: return "created"` | R9 | **By design** | Documented as "Best-effort" in docstring; "created" is the conservative safe default for a non-critical observability query |
| `retry_executor.py` `last_exc` possibly `None` at `raise` | R9 | **Not a bug** | `raise last_exc` is only reached after the loop exhausts all attempts; `last_exc` is always set before loop ends by at least one `except TransientExecutionError` |
| `planner.py` `abort_run` fallback for unrecognized recovery modes | R9 | **By design** | Intentional: unknown recovery mode → abort is the safe conservative path; not a silent failure |
| `llm_gateway.py` `OpenAILLMGateway` does not pass `idempotency_key` to provider API | R11 | **PoC scope** | Provider-side dedup via request-id header is a missed optimization; kernel-level dedup via `resolved_key` in ReasoningLoop is still enforced |
| `llm_gateway.py` `AnthropicLLMGateway._build_messages` wraps all history as user messages | R11 | **PoC scope** | Documented: "PoC wraps all history entries as user messages for simplicity" — production implementation should alternate roles |
| `output_parser.py` `uuid.uuid4().hex` for action_id when tool_call has no `id` | R11 | **Not a bug** | Parser runs inside TurnEngine, not in Temporal workflow replay path; action_id from parser is keyed off tool_call.id when present — UUID fallback is for malformed provider responses only |
| `dispatch_outbox_reconciler.py` `_repair_orphaned_key` catches `DedupeStoreStateError` only | R11 | **By design** | `OperationalError` from concurrent lock contention (DEF-021) propagates to caller — correct for transactional integrity; caller handles retry |
| `context_port.py`, `reflection_builder.py`, `mode_registry.py`, `client.py`, `adapter_ports.py` | R11 | **No defects** | Pure protocol/factory/mapping files; no mutable shared state; no exception swallowing |
| `gate.py` `_decide_reflect_and_retry` reflection `run_id` key scope | R7 | **No defect** | `_reflection_key` is scoped per `(run_id, based_on_offset, round)` — deterministic and correct |
| `LocalProcessScriptRuntime.execute_script` `timeout_ms` None guard | R7 | **Not a bug** | `ScriptActivityInput.timeout_ms: int = 30_000` — always int, never None |
| `compensation_registry.py` `has_handler` vs `lookup` inconsistency | R7 | **By design** | `has_handler` and `lookup` are deliberate separate API surface points |
| `bundle.py` / `gateway.py` / `activity_gateway.py` / `otel_export.py` | R7 | **No defects** | Clean boundary/translation layers |
| `kernel_runtime.py` `configure_run_actor_dependencies` double-register | R7 | **No defect** | Double-register with same bundle is idempotent; asyncio single-threaded; stop() correctly uses `clear_run_actor_dependencies(self._deps)` |
| `script_runtime_registry.py` module-level singleton init | R7 | **No defect** | Same import-caching argument as `action_type_registry.py`; safe in normal Python import model |
| `DedupeAwareScriptRuntime` noop result for "reserved" crashed state | R7 | **By design / PoC** | Known PoC limitation; reconciler surfaces stuck reserved keys |
| `_handle_signal` stale `next_offset` in commit | R3 | **Not a bug** | `append_action_commit` ignores incoming `commit_offset` and reassigns; stale value in commit object is cosmetic |
| `_seen_signal_tokens` reset on Temporal replay | R3 | **Mitigated by deduper** | `process_action_commit` dedup fingerprint via `_deduper` catches duplicates independently |
| `inference_activity.py` token budget enforcement | R8 | **No defect** | Logs warning on over-budget but proceeds — budget enforcement is correctly delegated to ContextPort |
| `observability_hooks.py` OtelObservabilityHook `token_usage.input_tokens` access | R8 | **Not a bug** | `ObservabilityHook.on_llm_call` protocol types `token_usage: TokenUsage \| None`; `TokenUsage` dataclass always has `input_tokens`, `output_tokens`, `reasoning_tokens`; direct access is correct |
| `observability_hooks.py` `CompositeObservabilityHook` swallows hook exceptions | R8 | **By design** | Documented: "Exceptions raised by any inner hook are swallowed individually so that one failing hook never silences the others" |
| `capability_snapshot.py` `_CURRENT_SNAPSHOT_SCHEMA_VERSION = "1"` | R8 | **No defect** | Internally consistent; `assert_snapshot_compatible` validates against this value; CLAUDE.md "schema_version=2" refers to binding fields present in v1 schema payload, not a different version |
| `capability_snapshot_resolver.py` `_first_non_empty_string` fallback | R8 | **No defect** | Graceful default chain: structured payload → flat payload → default; no ambiguity |
| `kernel_facade.py` `_is_expected_cancel_race_error` string matching | R8 | **No defect** | Defensive error-message matching with lowercase `.lower()` canonicalization; correct for race handling |
| `runner_adapter.py` `_extract_session_id` calls `getattr` twice for same attr | R8 | **No defect** | First call guards `callable(session_id_fn)`; second guards `isinstance(str)` — correct two-pass duck-typing pattern |
| `heartbeat.py` `make_health_check_fn` UNHEALTHY only when already in `_timed_out` | R8 | **By design** | DEGRADED = approaching/exceeded but signal not yet sent; UNHEALTHY = confirmed stuck (signal sent); intentional UX distinction |
| `heartbeat.py` `HeartbeatWatchdog._loop` bare `while True` without CancelledError guard | R8 | **No defect** | `asyncio.sleep` propagates `CancelledError` as `BaseException`, not caught by `except Exception` — clean cancellation by design |
| `action_type_registry.py` `validate_action_type` raises on `strict=True` | R8 | **No defect** | Opt-in strict mode for teams wanting to prevent ad-hoc action type pollution; default is warn-only |
| `skills/contracts.py` `SkillRuntimeHost` union vs `kernel/contracts.py` `HostKind` | R8 | **By design** | Skills layer defines its own 3-value union; `HostKind` in kernel contracts has 5 values — intentional tier boundary |
| `minimal_runtime.py` PoC implementations | R8 | **By design / PoC** | In-memory; not thread-safe; not for production; documented as such throughout |

---

## Appendix: Defect Cross-Reference

| Defect | Affects Production? | Affects Tests? | Detected by Existing Tests? |
|--------|---------------------|----------------|-----------------------------|
| DEF-001 | Yes — all `on_dedupe_hit` calls dead | Yes | No — hooks not asserted in tests |
| DEF-002 | Yes — multi-process SQLite | No | No — single-process tests only |
| DEF-003 | Yes — any concurrent cleanup scenario | No | No |
| DEF-004 | Yes — custom executor implementors | Yes | Possibly — depends on executor fixture |
| DEF-005 | Yes — concurrent multi-runtime Temporal workers | Yes | No |
| DEF-006 | Yes — rolling deploy / version bump | No | No — schema version never mismatches in tests |
| DEF-007 | Yes — any `cli_process`/`in_process_python` action | Yes | No — no test uses those host kinds |
| DEF-008 | Yes — all async contexts (entire production path) | Yes | No — reconciler not tested in async context |
| DEF-009 | Yes — concurrent reset during record_failure | No | No |
| DEF-010 | Yes — startup if duplicate versions passed | No | No |
| DEF-011 | Yes — any reasoning loop failure | Yes | Possibly — depends on reasoning loop mock |
| DEF-012 | Yes — event log always reports healthy | Yes | No — health probe not tested for failure path |
| DEF-013 | Yes — multi-worker same-process scenarios | No | No — single-worker tests only |
| DEF-014 | Yes — any multi-event commit via standalone event_log path | Possibly | No — autocommit behavior not tested |
| DEF-015 | Yes — multi-thread colocated bundle access | No | No |
| DEF-016 | Yes — any concurrent delete + mark_* | No | No |
| DEF-007b | Yes — any admission with cli_process/in_process_python envelope | Yes | No |
| DEF-017 | Yes — any parallel group that times out mid-execution | Yes | No — tests don't assert per-branch outcome after timeout |
| DEF-018 | Yes — dead code in crash-replay path; R6a recovery never triggers | Yes | No — branch_key format mismatch untested |
| DEF-019 | Yes — recovery circuit breaker for static_compensation never opens under sustained load | No | No |
| DEF-020 | Yes — any run with a compensation that raises; compensation permanently blocked | No | No |
| DEF-021 | Yes — multi-threaded shared bundle; OperationalError on concurrent mark_* | No | No |
| DEF-022 | Yes — reconciler receives false-negative report on I/O failure | No | No |
| DEF-023 | Yes — concurrent record_failure returns stale count, premature circuit trip | No | No |
| DEF-024 | Yes — post-commit-failure, all subsequent writes to same connection fail | No | No |
| DEF-025 | Yes — run_ids with underscores (common) cause cross-run key contamination | No | No |
| DEF-026 | Yes — any in-process script using stderr; ScriptResult.stderr always empty | Yes | No — InProcessPythonScriptRuntime stderr capture untested |
| DEF-027 | Yes — zombie processes on timeout; resource leak under sustained load | No | No — timeout path not tested with process teardown assertions |

---

## Scan Completion Status

**SCAN COMPLETE** — 2026-04-03 (Round 11 — all 60 files confirmed read)

All 60 `.py` source files across the following packages have been read and analyzed:

| Package | Files | Defects Found |
|---------|-------|---------------|
| `kernel/` (core FSM, contracts, snapshot) | 14 | DEF-001, DEF-004, DEF-006, DEF-007, DEF-007b, DEF-011 |
| `kernel/persistence/` | 8 | DEF-002, DEF-003, DEF-008, DEF-009, DEF-010, DEF-014, DEF-015, DEF-016, DEF-021, DEF-022, DEF-023, DEF-024, DEF-025 |
| `kernel/recovery/` | 5 | DEF-019, DEF-020 |
| `kernel/cognitive/` | 5 | DEF-026, DEF-027 |
| `kernel/plan_executor.py` | 1 | DEF-017, DEF-018 |
| `substrate/temporal/` | 5 | DEF-005, DEF-013 |
| `runtime/` | 6 | DEF-012 |
| `adapters/` | 7 | — |
| `skills/` | 2 | — |

**Total confirmed defects: 27** (DEF-001~020 Fixed; DEF-021~027 Open)  
**Severity breakdown (all)**: 1 CRITICAL, 15 HIGH, 8 MEDIUM, 3 LOW  
**Production impact**: All defects affect production paths. None are test-only.

---

## DEF-021 — `_SharedConnectionDedupeStore` mark_* methods: Python lock not held for full transaction span

**Severity**: CRITICAL  
**Category**: Concurrency / Transaction  
**File**: `agent_kernel/kernel/persistence/sqlite_colocated_bundle.py:224-289`  
**Status**: Open

### Evidence

`reserve()` correctly wraps its entire BEGIN→COMMIT span with `with self._lock:` (line 191).  
`mark_dispatched`, `mark_acknowledged`, `mark_unknown_effect` do NOT:

```python
def mark_dispatched(self, dispatch_idempotency_key, peer_operation_id=None):
    try:
        self._conn.execute("BEGIN IMMEDIATE")      # ← no lock held
        record = self._require(dispatch_idempotency_key)
        # _require() acquires+releases lock internally for just the SELECT
        if record.state not in ("reserved", "dispatched"):
            raise DedupeStoreStateError(...)
        self._update(...)
        # _update() acquires+releases lock internally for just the UPDATE
        self._conn.execute("COMMIT")               # ← no lock held
    except Exception:
        with contextlib.suppress(Exception):
            self._conn.execute("ROLLBACK")         # ← no lock held
        raise
```

The Python threading lock (`self._lock`) is held only within individual `_require()` and `_update()` calls — not for the full BEGIN→COMMIT span. Two concurrent threads can both attempt `BEGIN IMMEDIATE` on the same connection simultaneously. SQLite serializes write transactions via its own locking, so the second thread's `BEGIN IMMEDIATE` raises `sqlite3.OperationalError: database is locked` — **wrong exception type**. Callers (`TurnEngine`, `CompensationRegistry`, reconciler) catch `DedupeStoreStateError` only and do not handle `OperationalError`, so a busy-contention scenario causes an unhandled exception and a stuck workflow turn.

### Impact
Under multi-threaded worker load with shared `_SharedConnectionDedupeStore`, any concurrent state transition raises `OperationalError` in the second thread. The TurnEngine propagates this as an unhandled exception, aborting the turn. The dedupe key is left in its previous state (no rollback needed — the second thread never started a transaction), but the workflow turn exits with an internal error rather than a proper state machine failure.

### Fix
Wrap each `mark_*` method in `with self._lock:` at the outer level (identical to `reserve()`), and replace `_require()` (which has its own internal lock call) with the un-locked `_get()` directly:

```python
def mark_dispatched(self, dispatch_idempotency_key, peer_operation_id=None):
    with self._lock:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            record = self._get(dispatch_idempotency_key)
            if record is None:
                raise DedupeStoreStateError(f"Unknown key: {dispatch_idempotency_key}")
            if record.state not in ("reserved", "dispatched"):
                raise DedupeStoreStateError(
                    f"Cannot transition {record.state} -> dispatched."
                )
            self._conn.execute(
                "UPDATE colocated_dedupe_store SET state=?, peer_operation_id=?, "
                "external_ack_ref=? WHERE dispatch_idempotency_key=?",
                ("dispatched", peer_operation_id or record.peer_operation_id,
                 record.external_ack_ref, dispatch_idempotency_key),
            )
            if self._conn.execute(
                "SELECT changes()"
            ).fetchone()[0] != 1:
                raise DedupeStoreStateError(
                    f"Lost-update: key {dispatch_idempotency_key!r} disappeared."
                )
            self._conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise
```
Apply the same pattern to `mark_acknowledged` and `mark_unknown_effect`.

---

## DEF-022 — `averify_event_dedupe_consistency` silently swallows event log load failures

**Severity**: HIGH  
**Category**: Error swallowing / Silent failure  
**File**: `agent_kernel/kernel/persistence/consistency.py:220-223`  
**Status**: Open

### Evidence

```python
async def averify_event_dedupe_consistency(event_log, dedupe_store, run_id):
    report = ConsistencyReport(run_id=run_id)
    try:
        events = await event_log.load(run_id)
    except Exception:
        events = []          # ← swallows ALL load errors silently
    ...
    # With events=[], no idempotency_keys are found.
    # Every dedupe key therefore appears "orphaned" — or if the dedupe store
    # also fails, no violations are detected at all → false-negative report.
```

If `event_log.load()` raises (database locked, I/O error, JSON decode error), the function proceeds with `events = []`. All dedupe keys for the run then appear as having no matching event log entry (`orphaned_dedupe_key` violation), inflating the violation count. Conversely, if `_collect_dedupe_keys` also fails silently (it returns `[]` on exception), the function returns a clean report with `events_checked=0, dedupe_keys_checked=0` — a **false negative** that tells callers the state is consistent when it cannot actually verify that.

The sync variant has the same bug: lines 128-130 wrap `asyncio.run(event_log.load(run_id))` in a top-level `except Exception: pass` that silently sets `events = []`.

### Impact
`DispatchOutboxReconciler` depends on this function for correctness. A false-negative report causes the reconciler to skip all repairs. Orphaned "reserved" or "dispatched" dedupe keys are never transitioned to "unknown_effect", so the recovery gate never sees those failures. Actions that executed with unknown outcome are silently dropped.

### Fix
Surface event log failures explicitly. Either:
1. Re-raise the exception after logging so callers can handle it.
2. Add an `error` field to `ConsistencyReport` and set it when loading fails, so callers can detect the degraded state.

```python
try:
    events = await event_log.load(run_id)
except Exception as exc:
    _logger.error("averify: event_log.load(%r) failed: %s", run_id, exc)
    return ConsistencyReport(
        run_id=run_id,
        violations=[ConsistencyViolation(
            kind="event_log_unavailable",
            idempotency_key=None,
            dedupe_state=None,
            event_count=None,
            detail=f"event_log.load raised: {exc}",
        )],
    )
```

---

## DEF-023 — `SQLiteCircuitBreakerStore.record_failure()` UPSERT + SELECT not atomic; no threading lock

**Severity**: HIGH  
**Category**: Race condition / Non-atomic read-after-write  
**File**: `agent_kernel/kernel/persistence/sqlite_circuit_breaker_store.py:81-102`  
**Status**: Open

### Evidence

```python
def record_failure(self, effect_class: str) -> int:
    now = time.time()
    self._conn.execute(
        "INSERT INTO ... VALUES (?, 1, ?) ON CONFLICT DO UPDATE SET "
        "failure_count = failure_count + 1, ...",
        (effect_class, now),
    )
    self._conn.commit()                          # ← transaction ends here
    # ↑ Another thread can modify failure_count here ↑
    row = self._conn.execute(
        "SELECT failure_count ... WHERE effect_class = ?",
        (effect_class,),
    ).fetchone()                                 # ← reads potentially stale value
    ...
    return int(row[0])
```

`check_same_thread=False` is set (line 40) but no Python `threading.Lock` is used. Two threads calling `record_failure("external_api")` simultaneously can interleave:
1. Thread A: UPSERT (count=1), commit
2. Thread B: UPSERT (count=2), commit
3. Thread A: SELECT → reads `2` (Thread B's result) → returns `2`
4. Thread B: SELECT → reads `2` → returns `2`

Thread A believes the count is 2 and may immediately trip the circuit breaker even though only 1 failure is attributable to this call. Under sustained load each call reads a stale value, making the returned count effectively meaningless and potentially tripping the circuit breaker too early.

### Impact
Circuit breaker trips prematurely or unpredictably under concurrent load. Half-open probes are either granted too early (under-counting) or rejected too long (over-counting). This degrades recovery for any effect class that reaches the threshold.

### Fix
Two options:
1. Add a `threading.Lock` and wrap the UPSERT + SELECT in it:
   ```python
   self._lock = threading.Lock()
   # in record_failure:
   with self._lock:
       self._conn.execute("UPSERT ...")
       self._conn.commit()
       row = self._conn.execute("SELECT ...").fetchone()
   ```
2. Use a single SQL statement that both updates and returns the new count:
   ```sql
   INSERT INTO circuit_breaker_state (effect_class, failure_count, last_failure_ts)
   VALUES (?, 1, ?)
   ON CONFLICT(effect_class) DO UPDATE SET
       failure_count = failure_count + 1,
       last_failure_ts = excluded.last_failure_ts
   RETURNING failure_count
   ```

---

## DEF-024 — `SQLiteRecoveryOutcomeStore` and `SQLiteTurnIntentLog` write methods leave connection in open transaction on `commit()` failure

**Severity**: MEDIUM  
**Category**: Transaction / Resource leak  
**Files**: `agent_kernel/kernel/persistence/sqlite_recovery_outcome_store.py:25-49`,  
           `agent_kernel/kernel/persistence/sqlite_turn_intent_log.py:24-61`  
**Status**: Open

### Evidence

Both `write_outcome()` and `write_intent()` follow the same pattern:

```python
async def write_outcome(self, outcome: RecoveryOutcome) -> None:
    self._conn.execute("INSERT INTO recovery_outcome (...) VALUES (...)", (...))
    self._conn.commit()   # ← if this raises, INSERT is left un-committed
                          # ← no except/rollback — transaction remains open
```

Both connections use Python's default `isolation_level` (not `None`), meaning:
- Python's sqlite3 module implicitly begins a transaction before the first DML statement.
- `commit()` ends the transaction.
- If `commit()` raises (disk full, I/O error), the implicit transaction stays open.
- The next `INSERT` call raises `sqlite3.OperationalError: cannot start a transaction within a transaction`, making **all subsequent writes to the same connection permanently fail** until a rollback is issued.

### Impact
A transient `commit()` failure (disk full spike, SIGPIPE) permanently disables the store for the lifetime of the connection. Recovery outcomes and turn intents are silently dropped for all subsequent turns. No error is surfaced unless the caller inspects the exception from the next write attempt.

### Fix
Wrap each write method in `with self._conn:` (which auto-commits on success and auto-rolls-back on exception), or add explicit rollback:

```python
async def write_outcome(self, outcome: RecoveryOutcome) -> None:
    try:
        self._conn.execute("INSERT INTO recovery_outcome (...) VALUES (...)", (...))
        self._conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            self._conn.rollback()
        raise
```

Apply the same fix to `SQLiteTurnIntentLog.write_intent()`.

---

## DEF-025 — `_collect_dedupe_keys` uses unescaped `run_id` in SQLite LIKE — wildcard expansion corrupts key enumeration

**Severity**: LOW  
**Category**: Protocol / Incorrect query  
**File**: `agent_kernel/kernel/persistence/consistency.py:311`  
**Status**: Open

### Evidence

```python
rows = conn.execute(
    f"SELECT dispatch_idempotency_key FROM {table} "
    "WHERE dispatch_idempotency_key LIKE ?",
    (f"{run_id}:%",),          # ← run_id used verbatim as LIKE prefix
).fetchall()
```

SQLite LIKE wildcards are `%` (any sequence) and `_` (any single character). If `run_id` contains either of these characters — e.g., `run_test_001` or `run%staging` — the pattern `run_test_001:%` will match keys for `run-test-001:foo` and `run_testXXX:foo` (where `_` matches any char), pulling in keys that belong to **different** runs. This corrupts the key set fed to the consistency check.

This is particularly dangerous for `run_id` values containing underscores, which are common in many naming conventions (e.g., `workflow_abc_123`).

### Impact
Consistency check evaluates dedupe records from sibling runs. Violations are reported against the wrong run, causing spurious repair actions (orphaned keys incorrectly transitioned to `unknown_effect`) or missed violations (real orphans obscured by sibling keys).

### Fix
Escape the `run_id` before embedding in the LIKE pattern:

```python
# SQLite LIKE escape character: \ (backslash by default, or use ESCAPE clause)
escaped_run_id = run_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
rows = conn.execute(
    f"SELECT dispatch_idempotency_key FROM {table} "
    "WHERE dispatch_idempotency_key LIKE ? ESCAPE '\\'",
    (f"{escaped_run_id}:%",),
).fetchall()
```

---

## DEF-026 — `InProcessPythonScriptRuntime._run_sync` stderr not redirected — `ScriptResult.stderr` always empty

**Severity**: MEDIUM  
**Category**: Missing redirect — captured output loss  
**File**: `agent_kernel/kernel/cognitive/script_runtime.py:168-184`  
**Status**: Open

### Evidence

```python
def _run_sync(self, script_id, script_content, parameters, timeout_ms):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()           # ← created
    namespace = {"__params__": parameters}
    ...
    try:
        with redirect_stdout(stdout_buf):  # ← stdout redirected
            exec(script_content, namespace)  # ← stderr NOT redirected
    except ...

    stderr_str = stderr_buf.getvalue()   # ← always "" unless exception appended
    if exc is not None:
        stderr_str += f"{type(exc).__name__}: {exc}\n"
```

`stderr_buf` is created and read from, but `sys.stderr` is never redirected to it. The `contextlib.redirect_stderr` import is absent from the file. Any `print(..., file=sys.stderr)` or `sys.stderr.write(...)` call inside the executed script writes to the **actual process stderr**, bypassing the capture buffer entirely.

`ScriptResult.stderr` therefore contains only unhandled exception tracebacks (from the manual `exc` appending) — never intentional stderr output written by the script.

### Trigger

Any `InProcessPythonScriptRuntime` execution where the script writes to `sys.stderr` for diagnostics, progress reporting, or logging. This covers any script that uses standard Python idioms like `print("debug", file=sys.stderr)` or the `logging` module with a `StreamHandler` targeting stderr.

### Impact

- Script stderr output is silently discarded to the process stream.
- `ScriptResult.stderr` is always `""` for successfully-executing scripts, even when the script itself reported errors through stderr.
- Callers that inspect `ScriptResult.stderr` to detect script warnings or non-fatal errors receive a false-empty result, masking debugging signals.

### Fix

Add `redirect_stderr` to the import and wrap the `exec` call in both redirects:

```python
from contextlib import redirect_stdout, redirect_stderr

# In _run_sync:
try:
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        exec(script_content, namespace)
```

---

## DEF-027 — `LocalProcessScriptRuntime.execute_script` calls `proc.kill()` without `await proc.wait()` — zombie processes accumulate

**Severity**: LOW  
**Category**: Resource leak — zombie subprocess  
**File**: `agent_kernel/kernel/cognitive/script_runtime.py:273-274`  
**Status**: Open

### Evidence

```python
try:
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(),
        timeout=timeout_s,
    )
except TimeoutError:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()                   # ← sends SIGKILL, returns immediately
    elapsed_ms = int(...)
    return ScriptResult(              # ← returns without waiting for process exit
        ...
        exit_code=-1,
        ...
    )
```

After `proc.kill()`, the code returns immediately without calling `await proc.wait()`. The subprocess receives `SIGKILL` and terminates, but:

1. On Unix, until a `waitpid()` call is made, the process lingers as a zombie (entry visible in `ps aux` with state `Z`).
2. Python's asyncio subprocess transport will eventually call `waitpid()` via the SIGCHLD child watcher, but timing is non-deterministic and depends on the event loop running free.
3. Under sustained load with many timeout events, zombie processes accumulate between event loop ticks, creating a resource leak visible to `ps` and consuming process table entries.
4. The asyncio `SubprocessTransport` for the killed process is not explicitly closed, potentially holding file descriptor references for the PIPE pipes.

### Trigger

Any `LocalProcessScriptRuntime.execute_script` call where the script exceeds its `timeout_ms` budget.

### Impact

- Zombie processes accumulate on Unix hosts under repeated timeouts (e.g., runaway scripts in a high-throughput pipeline).
- File descriptors for stdout/stderr pipes may not be released until event loop cleanup.
- In container environments with strict PID limits, accumulated zombies can prevent new process creation.

### Fix

Add `await proc.wait()` after `proc.kill()` to ensure the OS resources are reaped before returning:

```python
except TimeoutError:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()          # ← reap zombie; discards returncode
    elapsed_ms = int(...)
    return ScriptResult(exit_code=-1, ...)
```
