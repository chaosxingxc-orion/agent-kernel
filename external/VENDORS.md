# Vendored Dependencies

| Package | Version | Upstream | Subtree prefix | Squash commit |
|---------|---------|----------|----------------|---------------|
| temporalio (sdk-python) | 1.24.0 | https://github.com/temporalio/sdk-python | `external/temporal-sdk-python` | 4cde647 |

## git subtree workflow

```bash
# Pull upstream tag upgrade (e.g. 1.25.0)
git subtree pull --prefix external/temporal-sdk-python \
  https://github.com/temporalio/sdk-python.git 1.25.0 --squash

# Push a local patch back to a personal fork
git subtree push --prefix external/temporal-sdk-python \
  https://github.com/<you>/sdk-python.git patch/my-fix
```

## Notes

- `external/temporal-sdk-python/` is the full SDK tree embedded via `git subtree add --squash`.
  It is excluded from the package build (hatchling), pyright type-checking, and ruff lint.
- Runtime dependency is pinned to `temporalio==1.24.0` in `pyproject.toml` to match the vendored copy.
  Install with: `pip install -e ".[temporal]"`

## Development modes

### Mode 1 — Vendored source (local patch or offline use)

`agent_kernel/substrate/temporal/_sdk_source.py::ensure_vendored_source()` is called
automatically before every `import temporalio.*` in the substrate layer.  It prepends
`external/temporal-sdk-python/temporalio` to `temporalio.__path__` so pure-Python
modules resolve from the local tree while the installed wheel provides the compiled
Rust extension (`temporalio.bridge.temporal_sdk_bridge`).

Use this mode when:
- Developing or testing local patches to the SDK without publishing to PyPI.
- Offline environments where the exact vendored version must be used.

No extra setup needed — just ensure `external/temporal-sdk-python/` is present (it is,
unless you manually deleted the subtree).

### Mode 2 — Remote Temporal service (production)

Connect agent-kernel to an external Temporal cluster by configuring `TemporalSubstrateConfig`:

```python
from agent_kernel.substrate.temporal.adaptor import TemporalAdaptor, TemporalSubstrateConfig

config = TemporalSubstrateConfig(
    mode="sdk",
    address="temporal.prod.example.com:7233",
    namespace="production",
    task_queue="agent-kernel",
)
adaptor = TemporalAdaptor(config)
await adaptor.start(deps)
```

The `ensure_vendored_source()` call is still made (idempotent), but if the vendored
tree is absent the installed wheel is used transparently.  In production deployments
the vendored subtree can be omitted from the Docker image entirely — only the
`temporalio==1.24.0` wheel is required.
