# agent-kernel 0.1 Release Notes

Release date: 2026-04-01

## Summary

`0.1` is the first stable baseline for the v6.4 architecture track.  
This release focuses on closing Phase A critical gaps: core contracts, durable persistence loops, guardrails, and regression coverage.

## Highlights

1. Core contract completeness
- Added `ExecutionContext` and `SandboxGrant`.
- Extended `AdmissionResult` with `sandbox_grant` and `idempotency_envelope`.
- Added skill runtime planning contracts including `ResolvedSkillPlan` and host-specific factory protocols.

2. Admission and dispatch hardening
- `TurnEngine` now treats `admit(action, snapshot)` as the primary admission path.
- Legacy `check(...)` remains as compatibility fallback and now emits a structured deprecation signal (`admission_legacy_check_fallback`).
- Added single-turn single-dispatch runtime guard.

3. Persistence closure
- Added SQLite `recovery_outcome` store.
- Added SQLite `turn_intent_log` store with idempotent upsert behavior.
- Runtime bundle now supports wiring both stores through backend configs.

4. Guardrails
- Enforced `derived_diagnostic` non-authority input protections in workflow/projection paths.
- Added recovery gate read-only boundary protection (no direct event-log mutation during recovery decisions).
- Added DedupeStore outage degradation path for `idempotent_write`.

5. Adapter boundary contracts
- Added and aligned kernel-level protocols:
  - `IngressAdapter`
  - `ContextBindingPort`
  - `CheckpointResumePort`
  - `CapabilityAdapter`

## Test Status

Baseline verification command:

```powershell
python -m pytest -q python_tests/agent_kernel
```

Result at baseline freeze: `251 passed`.

## Compatibility Notes

- `DispatchAdmissionService.check(...)` is still supported for compatibility but is deprecated.
- New integrations should implement and use `admit(action, snapshot)`.
- Legacy fallback emits a warning-level event and targets removal in `0.2`.

## Known Limits (Deferred to 0.2+)

- Full removal of legacy `check(...)` path (currently soft-deprecated).
- Extended observability around migration progress dashboards.
- Broader persistent/read-model parity checks for non-default projection implementations.

## Upgrade Guidance

1. Ensure admission implementations provide `admit(action, snapshot)`.
2. Monitor emitted state `admission_legacy_check_fallback`; any occurrence indicates pending migration work.
3. For durable deployments, enable SQLite backends for:
   - `recovery_outcome`
   - `turn_intent_log`
