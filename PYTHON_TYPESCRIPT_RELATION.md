# Runtime Scope

## Baseline Rule

This repository is Python-only for the `0.1` baseline.

1. Python (`python_src/agent_kernel`)
- Source of truth for v6.4 kernel authority model.
- Owns workflow orchestration, admission, dispatch, recovery, and persistence.

## De-scoped Content

TypeScript runtime paths and tests have been removed from this repository to
avoid dual-authority drift and review ambiguity.
