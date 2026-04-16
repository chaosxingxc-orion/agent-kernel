"""Pytest configuration for agent-kernel test suite.

This file adds environment-aware skipping for Temporal integration tests.
When Temporal's time-skipping test server cannot be started on the current
host (for example blocked by endpoint policy), Temporal integration tests are
auto-skipped instead of failing the entire suite.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

_TEMPORAL_INTEGRATION_HINTS: tuple[str, ...] = (
    "runtime/test_bundle_temporal_integration.py",
    "substrate/test_run_actor_workflow_temporal_integration.py",
    "substrate/test_temporal_gateway_integration.py",
)

_temporal_probe_result: tuple[bool, str] | None = None


def _should_consider_temporal_integration(item: pytest.Item) -> bool:
    """Should consider temporal integration."""
    node_path = str(item.nodeid).replace("\\", "/")
    return any(hint in node_path for hint in _TEMPORAL_INTEGRATION_HINTS)


def _temporal_probe() -> tuple[bool, str]:
    """Temporal probe."""
    global _temporal_probe_result
    if _temporal_probe_result is not None:
        return _temporal_probe_result

    # Explicit override knobs for CI and local debugging.
    if os.getenv("AGENT_KERNEL_SKIP_TEMPORAL_TESTS") == "1":
        _temporal_probe_result = (False, "AGENT_KERNEL_SKIP_TEMPORAL_TESTS=1")
        return _temporal_probe_result
    if os.getenv("AGENT_KERNEL_FORCE_TEMPORAL_TESTS") == "1":
        _temporal_probe_result = (True, "AGENT_KERNEL_FORCE_TEMPORAL_TESTS=1")
        return _temporal_probe_result

    try:
        from temporalio.testing import WorkflowEnvironment
    except Exception as exc:  # pragma: no cover - dependency not installed path
        _temporal_probe_result = (False, f"temporalio testing unavailable: {exc}")
        return _temporal_probe_result

    async def _probe_once() -> None:
        """Probe once."""
        async with await WorkflowEnvironment.start_time_skipping():
            return None

    try:
        asyncio.run(_probe_once())
        _temporal_probe_result = (True, "")
    except Exception as exc:
        _temporal_probe_result = (False, str(exc))
    return _temporal_probe_result


def pytest_collection_modifyitems(config: Any, items: list[pytest.Item]) -> None:
    """Pytest collection modifyitems."""
    del config
    supported, reason = _temporal_probe()
    if supported:
        return
    skip_marker = pytest.mark.skip(reason=f"Temporal integration skipped: {reason}")
    for item in items:
        if _should_consider_temporal_integration(item):
            item.add_marker(skip_marker)
