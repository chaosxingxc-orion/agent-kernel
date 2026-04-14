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
- Runtime dependency is still installed via `pip install -e ".[temporal]"` (`temporalio>=1.7,<2`).
  This vendored copy is for offline reference, local patching, and controlled upgrades.
