# Admission Legacy `check(...)` Deprecation Plan

## Scope

This document tracks the retirement path for the legacy admission API:

- Deprecated: `DispatchAdmissionService.check(action, projection)`
- Target path: `DispatchAdmissionService.admit(action, snapshot)`

## Current State (0.1)

1. Primary path is `admit(action, snapshot)`.
2. Legacy `check(...)` is compatibility fallback only.
3. Fallback emits structured signal:
- `state=admission_legacy_check_fallback`
- `severity=warning`
- `deprecation_phase=soft`
- `target_removal_version=0.2`

## Removal Gates

Before hard removal in `0.2`, all gates below should be green:

1. Zero fallback signals in integration and staging runs.
2. All custom admission adapters implement `admit(...)`.
3. Contract tests assert absence of `check(...)`-only implementations.

## Phased Plan

1. `0.1` (current): soft deprecation
- Keep fallback.
- Emit warning signal.

2. `0.1.x`: hardened migration
- Add CI test that fails on new `check(...)`-only adapters.
- Add migration dashboard metric from emitted fallback state.

3. `0.2`: hard deprecation
- Remove fallback branch in `TurnEngine`.
- Remove or no-op `check(...)` contract usage paths.
- Keep explicit migration note in release notes.
