# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
python -m pytest -q python_tests/agent_kernel

# Run a single test file
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py

# Run tests in a subdirectory
python -m pytest -q python_tests/agent_kernel/kernel/persistence

# Run a single test by name
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py::TestClassName::test_method_name

# Lint
ruff check agent_kernel/ python_tests/
ruff format agent_kernel/ python_tests/
pylint agent_kernel/
```

Pytest is configured in `pyproject.toml` with `pythonpath = ["."]` and `testpaths = ["python_tests"]`.

## Architecture

This is an enterprise agent kernel implementing a **six-authority lifecycle protocol** on top of Temporal. The key invariant: no authority can bypass another. All cross-layer communication uses kernel-safe frozen DTOs defined in `contracts.py`.

### Six Authorities

1. **RunActor** (`substrate/temporal/run_actor_workflow.py`) — Lifecycle authority; owns run progression. Temporal workflow that loops until run completion.
2. **RuntimeEventLog** (`kernel/minimal_runtime.py`, `kernel/persistence/`) — Append-only event truth; never mutated.
3. **DecisionProjectionService** — Projection truth; reconstructs state by replaying `authoritative_fact` and `derived_replayable` events (never `derived_diagnostic`).
4. **DispatchAdmissionService** — The ONLY gate before external side-effects. Write-class actions blocked when approval is pending/denied/revoked/expired.
5. **ExecutorService** — Execution authority; dispatches actions after admission.
6. **RecoveryGateService** — Failure recovery; writes to a separate `RecoveryOutcomeStore`, never mutates the event log.

### Core Data Flow

```
KernelFacade → TemporalGateway → RunActorWorkflow → TurnEngine
                                                    ├── CapabilitySnapshotBuilder (SHA256 hash)
                                                    ├── DispatchAdmissionService
                                                    ├── DedupeStore (at-most-once)
                                                    ├── ExecutorService
                                                    └── RecoveryGateService
```

`KernelFacade` (`adapters/facade/kernel_facade.py`) is the **only** allowed platform entrypoint. Never bypass it.

### Key Files

| File | Role |
|------|------|
| `kernel/contracts.py` | All DTOs and Protocol contracts. Read this first. Includes ConditionalPlan, DependencyGraph, SpeculativePlan, KernelManifest. |
| `kernel/turn_engine.py` | FSM canonical path. FSM diagram is at the top. |
| `kernel/minimal_runtime.py` | In-memory implementations of all protocols (PoC/tests only). |
| `kernel/capability_snapshot.py` | Deterministic SHA256 snapshot builder. |
| `kernel/capability_snapshot_resolver.py` | Approval-gate constraint enforcement. |
| `kernel/recovery/planner.py` | Heuristic failure→recovery routing. |
| `kernel/event_registry.py` | Central catalog of 25+ kernel event types. |
| `kernel/plan_type_registry.py` | Registry of ExecutionPlan types; aggregated into KernelManifest. |
| `kernel/action_type_registry.py` | Registry of action_type discriminators. |
| `adapters/facade/kernel_facade.py` | **Only** allowed platform entrypoint. get_manifest / submit_plan / submit_approval / commit_speculation / get_health. |
| `substrate/temporal/run_actor_workflow.py` | Temporal workflow with continue_as_new safety (threshold=10,000 rounds). |
| `substrate/temporal/worker.py` | Worker bootstrap; graceful SIGTERM/SIGINT shutdown. |
| `runtime/health.py` | K8s-style liveness/readiness probes. |
| `runtime/heartbeat.py` | Per-run timeout watchdog; injects signals via gateway (non-authority). |

### TurnEngine FSM States

```
collecting → intent_committed → snapshot_built → admission_checked
  → dispatch_blocked | dispatched → dispatch_acknowledged | effect_unknown
  → effect_recorded | recovery_pending → completed_noop
```

### Contracts & Patterns

**All DTOs are `@dataclass(frozen=True, slots=True)`** — mutation raises `FrozenInstanceError`.

**Snapshot hashing**: `CapabilitySnapshotBuilder` normalizes unordered lists (sort+dedupe) before SHA256. Same semantic inputs → same hash across processes. `schema_version="2"` required for model/memory/session/peer bindings.

**Approval gate constraint**: `approval_state` in `{pending, denied, revoked, expired}` → `permission_mode` forced to `"readonly"`. This is enforced in `capability_snapshot_resolver.py`.

**DedupeStore state machine**: `reserved → dispatched → acknowledged/unknown_effect`. No reversals. Enforces at-most-once dispatch semantics.

**Event authority levels**: `authoritative_fact` (replayed) / `derived_replayable` (replayed) / `derived_diagnostic` (never replayed, optionally filtered by facade).

**Dependency injection**: `RunActorWorkflow` receives a `RunActorDependencyBundle` via `ContextVar` (async) or thread-safe fallback dict. Tests configure with `configure_run_actor_dependencies()` — no conftest.py needed.

**No business logic in Temporal**: The workflow is a durable shell only. All logic lives in kernel services.

### Recovery Modes

Three modes in `RecoveryMode`: `static_compensation`, `human_escalation`, `abort`. Extensible via `KERNEL_RECOVERY_MODE_REGISTRY`. Outcomes are immutable once written to `RecoveryOutcomeStore`.

### Coding Standards

Follow the Google Python Style Guide. Ruff is configured with line length 100, targeting Python 3.14. Apply style fixes to code you touch ("incremental alignment").

### Known PoC Limitations

- In-memory implementations (`minimal_runtime.py`) are not for production.
- Peer signal authorization only checks `active_child_runs`; production tier uses `peer_run_bindings` in persisted snapshots.
- `DispatchAdmissionService.check()` is soft-deprecated; use `admit(action, snapshot)`.
- Heartbeat watchdog requires caller to invoke `monitor.watchdog_once()` periodically (no built-in scheduler).
