# CLAUDE.md

Guidance for Claude Code when working in this repository.

---

## Highest Principle — Language

**Always translate all instructions into English before passing them to any model.** This applies to task goals, prompts, tool arguments, and any text processed by an LLM. Never pass non-English text directly into a model call.

---

## Engineering Rules

Six non-negotiable rules. No exceptions.

**R1 — Think Before Coding.** State assumptions explicitly. If the requirement is ambiguous, stop and ask. Never pick an interpretation silently.

**R2 — Simplicity First.** Write the minimum code that solves the problem. No speculative features, unused abstractions, or unasked-for configurability.

**R3 — Surgical Changes.** Touch only what the task requires. Do not reformat, rename, or refactor adjacent code. Remove only imports/variables that *your* change made unused; leave pre-existing dead code alone.

**R4 — Goal-Driven Execution.** Convert vague tasks into falsifiable goals before starting. For multi-step work, publish a plan with per-step verification criteria and confirm before executing.

**R5 — Pre-Commit Inspection.** Before every commit, check each modified file across six dimensions:
| Dimension | Check |
|---|---|
| Contract truth | No `pass` / `raise NotImplementedError` stubs masquerading as real logic |
| Orphan parameters | Every constructor param / config field / env var is consumed downstream |
| Orphan return values | Every non-`None` return is used by the caller |
| Subsystem connectivity | No broken wiring, missing DI, or detached components in the call graph |
| Driver–result alignment | Every input that drives a decision produces an observable outcome |
| Error visibility | Every `except` re-raises, logs with context, or converts to a typed failure — no silent swallows |

**R6 — Three-Layer Testing.** After every implementation, all three layers must be green before the work is done:
- **Unit** — isolate new logic; mock only external network calls or fault injection (document the reason).
- **Integration** — real components wired together, no internal mocks; confirm side effects are produced.
- **End-to-end** — drive the public interface as a first-time user would; assert on observable outputs only.

---

## First Principles

- **P1** — The kernel must continuously evolve toward greater durability and correctness.
- **P2** — The cost of integrating with the kernel must continuously decrease.
- **P3** — No Mock implementations in production paths. (See constraint below.)

## Production Integrity Constraint (P3)

Using mocks to make tests pass by hiding real failures is **strictly forbidden**.

| Rule | Detail |
|---|---|
| No mock bypass | Do not use Mock/Stub/Fake to conceal missing components or misaligned interfaces |
| Tests reflect reality | A passing test must mean the real execution path works |
| Missing = exposed | Unimplemented dependencies → `@pytest.mark.skip(reason="awaiting real implementation")` or `xfail`, never a fake |
| Legitimate mock uses | (1) External network isolation in unit tests; (2) fault injection; (3) performance benchmarks — all must document the reason in the test docstring |
| Zero mocks in integration | Integration and E2E tests use real components only |

---

## Commands

```bash
# Run all tests
python -m pytest -q python_tests/agent_kernel

# Run a single test file
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py

# Run tests in a subdirectory
python -m pytest -q python_tests/agent_kernel/kernel/persistence

# Run a single test by name
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py::TestClass::test_method

# Lint
ruff check agent_kernel/ python_tests/
ruff format agent_kernel/ python_tests/
pylint agent_kernel/
```

Pytest is configured in `pyproject.toml` with `pythonpath = ["."]` and `testpaths = ["python_tests"]`.

---

## Architecture

Enterprise agent kernel implementing a **six-authority lifecycle protocol** on top of Temporal. Core invariant: no authority can bypass another. All cross-layer communication uses kernel-safe frozen DTOs from `contracts.py`.

### Six Authorities

| # | Authority | Role |
|---|---|---|
| 1 | **RunActor** (`substrate/temporal/run_actor_workflow.py`) | Lifecycle authority; owns run progression via Temporal workflow |
| 2 | **RuntimeEventLog** (`kernel/persistence/`) | Append-only event truth; never mutated |
| 3 | **DecisionProjectionService** | Reconstructs state by replaying `authoritative_fact` + `derived_replayable` events |
| 4 | **DispatchAdmissionService** | Only gate before external side-effects; blocks writes when approval is pending/denied/revoked/expired |
| 5 | **ExecutorService** | Dispatches actions after admission |
| 6 | **RecoveryGateService** | Failure recovery; writes to `RecoveryOutcomeStore`, never mutates the event log |

### Core Data Flow

```
KernelFacade → TemporalGateway → RunActorWorkflow → TurnEngine
                                                    ├── CapabilitySnapshotBuilder (SHA256)
                                                    ├── DispatchAdmissionService
                                                    ├── DedupeStore (at-most-once)
                                                    ├── ExecutorService
                                                    └── RecoveryGateService
```

`KernelFacade` (`adapters/facade/kernel_facade.py`) is the **only** allowed platform entrypoint. Never bypass it.

### Key Files

| File | Role |
|---|---|
| `kernel/contracts.py` | All DTOs and Protocol contracts — read this first |
| `kernel/turn_engine.py` | FSM canonical path; FSM diagram at the top |
| `kernel/minimal_runtime.py` | In-memory protocol implementations (PoC/tests only) |
| `kernel/capability_snapshot.py` | Deterministic SHA256 snapshot builder |
| `kernel/capability_snapshot_resolver.py` | Approval-gate constraint enforcement |
| `kernel/recovery/planner.py` | Heuristic failure→recovery routing |
| `kernel/event_registry.py` | Central catalog of 25+ kernel event types |
| `kernel/plan_type_registry.py` | ExecutionPlan type registry; aggregated into KernelManifest |
| `kernel/action_type_registry.py` | action_type discriminator registry |
| `adapters/facade/kernel_facade.py` | Only entrypoint: get_manifest / submit_plan / submit_approval / commit_speculation / get_health |
| `substrate/temporal/run_actor_workflow.py` | Temporal workflow; continue_as_new at 10,000 rounds |
| `substrate/temporal/worker.py` | Worker bootstrap; graceful SIGTERM/SIGINT shutdown |
| `runtime/health.py` | K8s-style liveness/readiness probes |
| `runtime/heartbeat.py` | Per-run timeout watchdog (non-authority) |

### TurnEngine FSM States

```
collecting → intent_committed → snapshot_built → admission_checked
  → dispatch_blocked | dispatched → dispatch_acknowledged | effect_unknown
  → effect_recorded | recovery_pending → completed_noop
```

### Contracts & Patterns

- **DTOs**: `@dataclass(frozen=True, slots=True)` — mutation raises `FrozenInstanceError`.
- **Snapshot hashing**: normalize (sort+dedupe) before SHA256; same semantic inputs → same hash. `schema_version="2"` required for model/memory/session/peer bindings.
- **Approval gate**: `approval_state` in `{pending, denied, revoked, expired}` → `permission_mode` forced to `"readonly"` (enforced in `capability_snapshot_resolver.py`).
- **DedupeStore**: `reserved → dispatched → acknowledged/unknown_effect`; no reversals; at-most-once dispatch.
- **Event levels**: `authoritative_fact` (replayed) / `derived_replayable` (replayed) / `derived_diagnostic` (never replayed).
- **Dependency injection**: `RunActorWorkflow` receives `RunActorDependencyBundle` via `ContextVar`; configure in tests with `configure_run_actor_dependencies()` — no conftest needed.
- **No business logic in Temporal**: the workflow is a durable shell only.

### Recovery Modes

`static_compensation` / `human_escalation` / `abort`. Extensible via `KERNEL_RECOVERY_MODE_REGISTRY`. Outcomes are immutable once written to `RecoveryOutcomeStore`.

### Coding Standards

Google Python Style Guide. Ruff: line length 100, target Python 3.14. Apply style fixes incrementally to code you touch.

### Known PoC Limitations

- `minimal_runtime.py` in-memory implementations are not for production.
- Peer signal auth checks only `active_child_runs`; production uses `peer_run_bindings` in persisted snapshots.
- `DispatchAdmissionService.check()` is soft-deprecated — use `admit(action, snapshot)`.
- Heartbeat watchdog requires the caller to invoke `monitor.watchdog_once()` periodically; no built-in scheduler.
