# Vendored Dependencies

| Package | Version | Source | Commit | License |
|---------|---------|--------|--------|---------|
| temporalio | 1.24.0 | https://github.com/temporalio/sdk-python | 8f003b4 | MIT |

## Notes

- `temporalio/` — Temporal Python SDK source, vendored at tag `1.24.0`.
  Kept for offline reference and local patching. The installed package
  (`pip install -e ".[temporal]"`) takes precedence at runtime; this copy
  is not on `sys.path` by default.
- To apply a local patch: `pip install -e external/temporalio` (requires
  the SDK's own `[build-system]` to be present — see `temporalio_pyproject.toml`).
- Upgrade procedure: copy new tag's `temporalio/` directory here and update
  the version row above.
