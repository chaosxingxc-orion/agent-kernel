# agent-kernel

Python-first `0.1` baseline for the v6.4 agent execution kernel.

## Scope

- Runtime source of truth: `python_src/agent_kernel`
- Test suite: `python_tests/agent_kernel`
- Upstream dependencies: `external/` (git submodules)
- TypeScript runtime path is intentionally removed from this repository baseline.

## Quick Start

```bash
git clone https://github.com/chaosxingxc-orion/agent-kernel.git
cd agent-kernel
git submodule update --init --recursive
python -m pip install -U pip pytest ruff pylint
```

## Validation

```bash
# tests
python -m pytest -q python_tests/agent_kernel

# lint
python -m ruff check python_src python_tests
python -m pylint python_src python_tests
```

Or via npm script wrappers:

```bash
npm run test:py
npm run lint:py
```

## CI

GitHub Actions runs Python-only lint and test gates:

- `.github/workflows/ci-lint.yml`
- `.github/workflows/ci-test.yml`

## Release Notes

See:

- `RELEASE_NOTES_0.1.md`
- `V6_4_GAP_ASSESSMENT.md`
