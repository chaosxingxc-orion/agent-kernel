# agent-kernel Refactoring Design

**Date:** 2026-04-08  
**Status:** Approved  
**Goal:** Reduce agent-kernel to a pure execution substrate — run lifecycle + event log + TurnEngine primitives — moving all plan orchestration and business logic to hi-agent.

---

## Context

agent-kernel was originally designed with five Plan types (SequentialPlan / ParallelPlan / ConditionalPlan / DependencyGraph / SpeculativePlan) and a PlanExecutor (753 lines) that owned scheduling logic. hi-agent's AsyncTaskScheduler now owns graph-based task scheduling, making the kernel's Plan layer redundant.

The refactoring preserves both substrates (LocalFSM + Temporal), keeps the full observability/query API, and increments in four independent phases — each phase leaves tests green and the system deployable.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Substrate strategy | Keep both (LocalFSM + Temporal) | Temporal needed for durable production execution |
| Plan types | Migrate to hi-agent | Semantics preserved as TrajectoryGraph builders |
| KernelFacade API | Two-layer (primitives + observability) | Clean separation, no big-bang API break |
| TaskManager | Split — TaskRegistry stays, RestartPolicy/Reflection moves | Kernel tracks state, hi-agent owns decisions |
| Execution strategy | Incremental layering (4 phases) | Each phase independently deployable |

---

## Target Architecture

### agent-kernel (post-refactor)

```
KernelFacade
├── Core Primitives
│   ├── start_run(request) → StartRunResponse
│   ├── execute_turn(run_id, action, handler, *, idempotency_key) → TurnResult
│   ├── subscribe_events(run_id) → AsyncIterator[RuntimeEvent]
│   ├── signal_run(request) → None
│   └── terminate_run(run_id, reason) → None
│
├── Observability / Query (read-only, unchanged)
│   ├── query_run / query_run_dashboard / query_trace_runtime
│   ├── stream_run_events / query_run_postmortem
│   └── spawn_child_run
│
├── TRACE Alignment (unchanged)
│   ├── open_stage / mark_stage_state / open_branch / mark_branch_state
│   └── submit_approval / open_human_gate
│
├── TaskRegistry (lightweight state tracking)
│   └── register_task / get_task_status / list_session_tasks
│
└── Health / Manifest
    └── get_manifest / get_health / get_health_readiness

Substrate Layer (unchanged, encapsulated)
├── LocalFSMAdaptor  — asyncio, in-process, no durability
└── TemporalAdaptor  — durable execution, cross-process, HA
```

### Removed from agent-kernel

```
agent_kernel/kernel/plan_executor.py          ← deleted
agent_kernel/kernel/plan_type_registry.py     ← deleted
contracts.py: SequentialPlan / ParallelPlan / ConditionalPlan
             / DependencyGraph / SpeculativePlan  ← removed
KernelFacade: submit_plan() / commit_speculation()  ← removed
kernel/task_manager/restart_policy.py         ← moved to hi-agent
kernel/task_manager/reflection_orchestrator.py ← moved to hi-agent
```

### Migrated to hi-agent

```
hi_agent/task_mgmt/plan_types.py
    — SequentialPlan / ParallelPlan / DependencyGraph
      / ConditionalPlan / SpeculativePlan
    — plan_to_graph(plan) → TrajectoryGraph

hi_agent/task_mgmt/restart_policy.py
    — RestartPolicyEngine (immediate / backoff / abandon strategies)

hi_agent/task_mgmt/reflection.py
    — ReflectionOrchestrator
    — on_node_failed(scheduler, failed_node, reason) → injects repair nodes
      via scheduler.add_node()
```

---

## Interface Contract

### execute_turn() — the new core primitive

```python
AsyncActionHandler = Callable[[Action, str | None], Awaitable[Any]]

async def execute_turn(
    self,
    run_id: str,
    action: Action,
    handler: AsyncActionHandler,
    *,
    idempotency_key: str,
) -> TurnResult:
    """
    Atomic execution unit. Kernel is responsible for:
      Admission check → Dedupe → await handler(action, sandbox_grant) → Record event

    handler is provided by hi-agent (business logic).
    Kernel guarantees exactly-once execution via idempotency_key.
    """
```

**LocalFSM substrate:** `await handler(action, None)` in-process, result written to in-memory EventLog.

**Temporal substrate:** handler registered as Temporal Activity; signal_workflow triggers execution; Temporal guarantees durability and exactly-once across process restarts.

### plan_to_graph() — Plan migration bridge

```python
# hi_agent/task_mgmt/plan_types.py
def plan_to_graph(plan: AnyPlan) -> TrajectoryGraph:
    """Convert any Plan type to a TrajectoryGraph for AsyncTaskScheduler."""
    if isinstance(plan, SequentialPlan): ...
    elif isinstance(plan, ParallelPlan): ...
    elif isinstance(plan, DependencyGraph): ...
    elif isinstance(plan, ConditionalPlan): ...
    elif isinstance(plan, SpeculativePlan): ...
```

### ReflectionOrchestrator — dynamic graph repair

```python
# hi_agent/task_mgmt/reflection.py
class ReflectionOrchestrator:
    def on_node_failed(
        self,
        scheduler: AsyncTaskScheduler,
        failed_node: TrajNode,
        failure_reason: str,
    ) -> None:
        # Analyze failure → generate repair node → inject via scheduler.add_node()
        repair_node = TrajNode(node_id=f"{failed_node.node_id}:repair", ...)
        scheduler.add_node(repair_node, depends_on=[])
```

---

## Execution Phases

### Phase 1: Add execute_turn() to KernelFacade
**Risk:** Low — additions only, no deletions.

- Add `AsyncActionHandler` type alias to `agent_kernel/kernel/contracts.py`
- Implement `execute_turn()` on LocalFSMAdaptor (direct handler invocation via TurnEngine path)
- Implement `execute_turn()` on TemporalAdaptor (Activity registration + signal-based invocation)
- Expose `execute_turn()` on KernelFacade
- Tests: `python_tests/agent_kernel/test_execute_turn_local.py` + `test_execute_turn_facade.py`
- **Completion gate:** hi-agent can replace MockKernelFacade with real KernelFacade, all 1997 tests green

### Phase 2: Migrate Plan types to hi-agent
**Risk:** Medium — import updates across hi-agent.

- Create `hi_agent/task_mgmt/plan_types.py` with all 5 Plan types + `plan_to_graph()`
- Mark Plan types in `agent_kernel/kernel/contracts.py` as deprecated (keep definitions for now)
- Mark `plan_type_registry.py` as deprecated
- Tests: `tests/test_plan_types.py` verifying `plan_to_graph()` produces correct topology for each Plan type
- **Completion gate:** hi-agent 1997 tests green; agent-kernel 7203 tests green

### Phase 3: Delete PlanExecutor and Plan API
**Risk:** Medium — large test deletion in agent-kernel.

- Step 1: Count and list all tests in `python_tests/` that reference PlanExecutor or Plan types
- Step 2: Delete `plan_executor.py`, `plan_type_registry.py`
- Step 3: Remove Plan classes from `contracts.py`
- Step 4: Remove `submit_plan()` shim and `commit_speculation()` from KernelFacade
- Step 5: Delete corresponding plan-specific tests
- **Completion gate:** remaining agent-kernel tests (estimated ~6800+) all green

### Phase 4: Decouple TaskManager
**Risk:** Low — clean interface boundaries.

- Move `kernel/task_manager/restart_policy.py` → `hi_agent/task_mgmt/restart_policy.py`
- Move `kernel/task_manager/reflection_orchestrator.py` → `hi_agent/task_mgmt/reflection.py`
- Update `ReflectionOrchestrator.on_node_failed()` to use `AsyncTaskScheduler.add_node()`
- Remove deleted files from `kernel/task_manager/`; keep `TaskRegistry`, `TaskDescriptor`, `TaskHealthStatus`, `TaskWatchdog`
- Tests for new hi-agent modules
- **Completion gate:** all tests green; `TaskWatchdog` only reports state, never triggers business actions

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Temporal Activity registration breaks execute_turn() | Medium | Test with local Temporal dev-server before merging |
| Plan type removal breaks undiscovered consumers | Low | grep all references before Phase 3 Step 2 |
| ReflectionOrchestrator depends on kernel internals | Low | Audit imports before move; inject via add_node() only |
| Phase 3 test deletion misses coverage gaps | Medium | Run mutation testing on core TurnEngine after Phase 3 |

---

## Success Criteria

- agent-kernel has zero Plan execution code after Phase 3
- `execute_turn()` works identically on LocalFSM and Temporal substrates
- MockKernelFacade and real KernelFacade implement the same protocol
- hi-agent AsyncTaskScheduler runs against real KernelFacade without code changes
- All remaining tests green after each phase
