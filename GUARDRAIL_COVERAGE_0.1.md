# Guardrail Coverage Report (0.1)

## Objective

Record which v6.4 guardrails are now enforced in runtime behavior for the `0.1` baseline.

## Enforced Guardrails

1. Single turn, single authoritative dispatch
- Enforced in `TurnEngine` with runtime dispatch-attempt counter assertion.

2. `derived_diagnostic` must not flow into authority input
- Enforced in workflow and projection/replay paths.
- Violations raise deterministic errors.

3. Recovery gate cannot directly mutate lifecycle/event truth
- Recovery decision path wrapped by read-only event-log guard during `decide(...)`.

4. Dedupe outage degradation
- If dedupe reserve fails for `idempotent_write`, execution degrades to:
  - `compensatable_write` for local hosts
  - `irreversible_write` for remote hosts

5. Admission migration visibility
- Legacy `check(...)` fallback emits structured warning event for migration tracking.

## Test Evidence (Representative)

- `python_tests/agent_kernel/kernel/test_turn_engine.py`
- `python_tests/agent_kernel/kernel/test_minimal_runtime.py`
- `python_tests/agent_kernel/substrate/test_run_actor_workflow.py`
- `python_tests/agent_kernel/runtime/test_bundle.py`

Baseline full regression:

```powershell
python -m pytest -q python_tests/agent_kernel
```

Result at report time: `251 passed`.

## Remaining Hardening (Post-0.1)

1. Remove legacy `check(...)` fallback in `0.2`.
2. Expand projection guardrails to additional backend implementations.
3. Add automated guardrail observability counters to release dashboards.
