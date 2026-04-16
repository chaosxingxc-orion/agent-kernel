"""Verifies for tool/mcp adapter alignment with openjiuwen-like metadata."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent_kernel.adapters.agent_core.tool_mcp_adapter import AgentCoreToolMCPAdapter
from agent_kernel.kernel.contracts import Action, EffectClass


@dataclass(frozen=True, slots=True)
class _FakeToolInfo:
    """Test suite for  FakeToolInfo."""

    name: str


@dataclass(frozen=True, slots=True)
class _FakeMcpToolInfo:
    """Test suite for  FakeMcpToolInfo."""

    name: str
    server_name: str


def _make_action(input_json: dict | None = None) -> Action:
    """Make action."""
    return Action(
        action_id="action-1",
        run_id="run-1",
        action_type="default_action",
        effect_class=EffectClass.READ_ONLY,
        input_json=input_json,
    )


def test_resolve_tool_from_openjiuwen_tool_info_object() -> None:
    """Verifies resolve tool from openjiuwen tool info object."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_tool(
            _make_action({"capability_scope": ["search", "read"]}),
            _FakeToolInfo(name="web_search"),
        )
    )

    assert binding.tool_id == "web_search"
    assert binding.handler_ref == "agent_core.tool.web_search"
    assert binding.capability_scope == ["search", "read"]


def test_resolve_tool_falls_back_to_action_payload_and_type() -> None:
    """Verifies resolve tool falls back to action payload and type."""
    adapter = AgentCoreToolMCPAdapter()
    payload_binding = asyncio.run(adapter.resolve_tool(_make_action({"name": "filesystem_read"})))
    fallback_binding = asyncio.run(adapter.resolve_tool(_make_action()))

    assert payload_binding.tool_id == "filesystem_read"
    assert fallback_binding.tool_id == "default_action"


def test_resolve_mcp_from_mcp_tool_info_object() -> None:
    """Verifies resolve mcp from mcp tool info object."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_mcp(
            _make_action(),
            _FakeMcpToolInfo(name="search_docs", server_name="docs_server"),
        )
    )

    assert binding.server_id == "docs_server"
    assert binding.capability_id == "search_docs"


def test_resolve_mcp_from_action_payload() -> None:
    """Verifies resolve mcp from action payload."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_mcp(
            _make_action(
                {
                    "mcp": {
                        "server_name": "code_server",
                        "capability_id": "code_search",
                    }
                }
            )
        )
    )

    assert binding.server_id == "code_server"
    assert binding.capability_id == "code_search"


def test_resolve_tool_normalizes_capability_scope() -> None:
    """Verifies resolve tool normalizes capability scope."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_tool(
            _make_action(
                {
                    "name": "filesystem_read",
                    "capability_scope": [
                        "search",
                        " read ",
                        "",
                        "search",
                        None,
                        "read",
                    ],
                }
            )
        )
    )

    assert binding.capability_scope == ["search", "read"]


def test_resolve_tool_supports_single_capability_field() -> None:
    """Verifies resolve tool supports single capability field."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_tool(
            _make_action(
                {
                    "name": "filesystem_read",
                    "capability": " read ",
                }
            )
        )
    )

    assert binding.capability_scope == ["read"]


def test_resolve_mcp_supports_schema_and_credential_fallback_from_mcp_payload() -> None:
    """Verifies resolve mcp supports schema and credential fallback from mcp payload."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_mcp(
            _make_action(
                {
                    "mcp": {
                        "server_name": "docs_server",
                        "capability_id": "query_docs",
                        "schema_ref": "schema://docs-query",
                        "credential_boundary_ref": "cred://docs-readonly",
                    }
                }
            )
        )
    )

    assert binding.server_id == "docs_server"
    assert binding.capability_id == "query_docs"
    assert binding.schema_ref == "schema://docs-query"
    assert binding.credential_boundary_ref == "cred://docs-readonly"


def test_resolve_mcp_schema_prefers_metadata_and_falls_back_per_field() -> None:
    """Verifies resolve mcp schema prefers metadata and falls back per field."""
    adapter = AgentCoreToolMCPAdapter()
    binding = asyncio.run(
        adapter.resolve_mcp(
            _make_action(
                {
                    "mcp": {
                        "server_name": "docs_server",
                        "capability_id": "query_docs",
                        "schema_ref": "schema://payload",
                        "credential_boundary_ref": "cred://payload",
                    }
                }
            ),
            {
                "server_name": "docs_server",
                "name": "query_docs",
                "schema_ref": "schema://explicit",
            },
        )
    )

    assert binding.schema_ref == "schema://explicit"
    assert binding.credential_boundary_ref == "cred://payload"


def test_resolve_mcp_supports_server_and_capability_alias_fields() -> None:
    """Verifies resolve mcp supports server and capability alias fields."""
    adapter = AgentCoreToolMCPAdapter()
    nested_binding = asyncio.run(
        adapter.resolve_mcp(
            _make_action({"mcp": {"server": "alias_server", "capability": "alias_capability"}})
        )
    )
    top_level_binding = asyncio.run(
        adapter.resolve_mcp(_make_action({"server": "top_server", "capability": "top_capability"}))
    )

    assert nested_binding.server_id == "alias_server"
    assert nested_binding.capability_id == "alias_capability"
    assert top_level_binding.server_id == "top_server"
    assert top_level_binding.capability_id == "top_capability"
