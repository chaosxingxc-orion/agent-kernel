"""Large stability matrix for activity-backed executor route invariants."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_kernel.kernel.contracts import Action, MCPActivityInput, ToolActivityInput
from agent_kernel.kernel.minimal_runtime import ActivityBackedExecutorService

_CASE_COUNT = 1000


@dataclass(slots=True)
class _Gateway:
    tool_requests: list[ToolActivityInput] = field(default_factory=list)
    mcp_requests: list[MCPActivityInput] = field(default_factory=list)

    async def execute_tool(self, request: ToolActivityInput) -> dict[str, Any]:
        self.tool_requests.append(request)
        return {"route": "tool"}

    async def execute_mcp(self, request: MCPActivityInput) -> dict[str, Any]:
        self.mcp_requests.append(request)
        return {"route": "mcp"}


def _action_for(seed: int) -> Action:
    if seed % 3 == 0:
        return Action(
            action_id=f"a-{seed}",
            run_id=f"r-{seed}",
            action_type="mcp.query",
            effect_class="read_only",
            input_json={},
        )
    if seed % 3 == 1:
        return Action(
            action_id=f"a-{seed}",
            run_id=f"r-{seed}",
            action_type="tool.search",
            effect_class="read_only",
            input_json={
                "mcp": {
                    "server_name": "alpha",
                    "capability_id": "list",
                }
            },
        )
    return Action(
        action_id=f"a-{seed}",
        run_id=f"r-{seed}",
        action_type="tool.search",
        effect_class="read_only",
        input_json={"query": f"q-{seed}"},
    )


@pytest.mark.parametrize("seed", list(range(_CASE_COUNT)))
def test_executor_routing_matrix(seed: int) -> None:
    """Executor routing should remain deterministic for route trigger patterns."""
    gateway = _Gateway()
    executor = ActivityBackedExecutorService(gateway)
    action = _action_for(seed)

    result = asyncio.run(executor.execute(action))

    if seed % 3 in (0, 1):
        assert result["route"] == "mcp"
        assert len(gateway.mcp_requests) == 1
        assert len(gateway.tool_requests) == 0
    else:
        assert result["route"] == "tool"
        assert len(gateway.tool_requests) == 1
        assert len(gateway.mcp_requests) == 0
