# V6.4 Gap Assessment (Current vs Target)

## Evidence Baseline (2026-03-31)
- Python suite: `python -m pytest -q` -> `209 passed`.
- TypeScript suite: `npm test -- --run` -> `11 passed` (4 files).
- Strict/runtime config wiring and remote-service audit/idempotency payload enforcement are now in the canonical path.

## Closed Since Last Assessment
- Strict snapshot-input + declarative bundle digest enforcement is wired end-to-end.
  - `python_src/agent_kernel/kernel/capability_snapshot_resolver.py`
  - `python_src/agent_kernel/substrate/temporal/run_actor_workflow.py`
  - `python_tests/agent_kernel/substrate/test_run_actor_workflow.py`
- Remote-service idempotency/audit payload policy is enforced in turn dispatch decisions.
  - `python_src/agent_kernel/kernel/turn_engine.py`
  - `python_src/agent_kernel/kernel/remote_service_policy.py`
  - `python_tests/agent_kernel/kernel/test_turn_engine.py`
- TypeScript runtime stubs are implemented; core runtime tests are green.
  - `src/core/eventlog/event_log.ts`
  - `src/core/actor/run_actor.ts`
  - `src/operations/snapshot/replay_engine.ts`
  - `src/operations/lifecycle/run_lifecycle.ts`

## Remaining Gaps

### P0
- None.

### P1
- Add Temporal integration assertions for lifecycle precedence under race/replay (`Cancel > HardFailure > Timeout > ExternalCallback`).
  - `python_tests/agent_kernel/substrate/test_run_actor_workflow_temporal_integration.py`
  - `python_tests/agent_kernel/runtime/test_bundle_temporal_integration.py`

### P2
- Add cross-runtime integration coverage that exercises TS actor/eventlog/snapshot/lifecycle together (current TS coverage is unit-level only).
  - `tests/core/actor/run_actor.test.ts`
  - `tests/core/eventlog/event_log.test.ts`
  - `tests/operations/snapshot/replay_engine.test.ts`
  - `tests/operations/lifecycle/run_lifecycle.test.ts`
