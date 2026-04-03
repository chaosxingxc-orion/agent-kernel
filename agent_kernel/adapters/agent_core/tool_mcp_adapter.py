"""Tool/MCP adapter for aligning openjiuwen metadata with kernel bindings.

Design intent:
  - Convert heterogeneous platform metadata into normalized binding DTOs used
    by kernel execution components.
  - Preserve agent_kernel separation of concerns: adapter performs *mapping only*.

Architectural boundary:
  - No admission decisions here.
  - No side effects here.
  - No lifecycle/event truth updates here.

Principle:
  - Prefer explicit metadata from platform objects.
  - Fall back to action payload hints for compatibility.
  - Fall back to deterministic defaults to avoid ambiguous null bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import Action


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """Represents one resolved tool binding for kernel execution.

    Attributes:
        tool_id: Stable logical tool identifier selected for execution.
        handler_ref: Runtime handler reference consumed by executor runtime.
        capability_scope: Optional scope tags used for governance checks.
    """

    tool_id: str
    handler_ref: str
    capability_scope: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MCPBinding:
    """Represents one resolved MCP binding for kernel execution.

    Attributes:
        server_id: MCP server identifier.
        capability_id: Capability exposed by the target MCP server.
        schema_ref: Optional schema reference for payload validation.
        credential_boundary_ref: Optional credential boundary descriptor.
    """

    server_id: str
    capability_id: str
    schema_ref: str | None = None
    credential_boundary_ref: str | None = None


class AgentCoreToolMCPAdapter:
    """Maps action metadata to tool/MCP bindings.

    The adapter accepts either openjiuwen-style objects (for example,
    `ToolInfo` / `McpToolInfo`) or kernel action payload hints.
    """

    async def resolve_tool(
        self,
        action: Action,
        tool_info: Any | None = None,
    ) -> ToolBinding:
        """Resolves a tool binding from optional metadata and action payload.

        Resolution order:
          1. Explicit ``tool_info`` object/dict.
          2. Action payload hints (``input_json``).
          3. ``action.action_type`` as deterministic fallback.

        Args:
            action: Kernel action currently being prepared for execution.
            tool_info: Optional openjiuwen-like metadata object.

        Returns:
            A normalized tool binding for downstream executor components.
        """
        resolved_tool_id = self._extract_tool_name(tool_info)
        if resolved_tool_id is None:
            resolved_tool_id = self._extract_tool_name(action.input_json)
        if resolved_tool_id is None:
            resolved_tool_id = action.action_type
        return ToolBinding(
            tool_id=resolved_tool_id,
            handler_ref=f"agent_core.tool.{resolved_tool_id}",
            capability_scope=self._extract_capability_scope(action.input_json),
        )

    async def resolve_mcp(
        self,
        action: Action,
        mcp_info: Any | None = None,
    ) -> MCPBinding:
        """Resolves an MCP binding from optional metadata and action payload.

        Resolution order:
          1. Explicit ``mcp_info`` object/dict.
          2. Action payload ``input_json['mcp']`` hints.
          3. Deterministic defaults.

        Args:
            action: Kernel action currently being prepared for execution.
            mcp_info: Optional openjiuwen-like MCP metadata object.

        Returns:
            A normalized MCP binding object.
        """
        server_id = self._extract_server_name(mcp_info)
        capability_id = self._extract_capability_id(mcp_info)
        mcp_payload = self._extract_mcp_payload(action.input_json)
        if server_id is None or capability_id is None:
            if server_id is None:
                server_id = self._extract_server_name(mcp_payload)
            if capability_id is None:
                capability_id = self._extract_capability_id(mcp_payload)

        # Backward-compatible payload fallback:
        # some callers provide MCP hints directly at the top-level input_json
        # rather than nesting under "mcp".
        if server_id is None:
            server_id = self._extract_server_name(action.input_json)
        if capability_id is None:
            capability_id = self._extract_capability_id(action.input_json)

        if server_id is None:
            server_id = "default_mcp_server"
        if capability_id is None:
            capability_id = action.action_type

        schema_ref = self._extract_optional_str(mcp_info, "schema_ref")
        if schema_ref is None:
            schema_ref = self._extract_optional_str(mcp_payload, "schema_ref")

        credential_boundary_ref = self._extract_optional_str(
            mcp_info,
            "credential_boundary_ref",
        )
        if credential_boundary_ref is None:
            credential_boundary_ref = self._extract_optional_str(
                mcp_payload,
                "credential_boundary_ref",
            )

        return MCPBinding(
            server_id=server_id,
            capability_id=capability_id,
            schema_ref=schema_ref,
            credential_boundary_ref=credential_boundary_ref,
        )

    async def resolve_tool_bindings(self, action: Action) -> list[ToolBinding]:
        """CapabilityAdapter-compatible tool bindings resolver.
        Args:
            action: (description)
        Returns:
            list[ToolBinding]: (description)
        """
        return [await self.resolve_tool(action)]

    async def resolve_mcp_bindings(self, action: Action) -> list[MCPBinding]:
        """CapabilityAdapter-compatible MCP bindings resolver.
        Args:
            action: (description)
        Returns:
            list[MCPBinding]: (description)
        """
        return [await self.resolve_mcp(action)]

    async def resolve_skill_bindings(self, action: Action) -> list[str]:
        """Resolves skill binding references from action payload hints.
        Args:
            action: (description)
        Returns:
            list[str]: (description)
        """
        payload = action.input_json if isinstance(action.input_json, dict) else {}
        raw_bindings = payload.get("skill_bindings")
        if isinstance(raw_bindings, list):
            return [str(value).strip() for value in raw_bindings if str(value).strip() != ""]
        raw_single = payload.get("skill_binding")
        if raw_single is None:
            return []
        token = str(raw_single).strip()
        return [token] if token != "" else []

    async def resolve_declarative_bundle(self, action: Action) -> dict[str, str] | None:
        """Resolves declarative bundle digest payload from action input JSON.
        Args:
            action: (description)
        Returns:
            dict[str, str] | None: (description)
        """
        payload = action.input_json if isinstance(action.input_json, dict) else {}
        bundle = payload.get("declarative_bundle_digest")
        if not isinstance(bundle, dict):
            return None
        required_fields = ("bundle_ref", "semantics_version", "content_hash", "compile_hash")
        normalized: dict[str, str] = {}
        for field_name in required_fields:
            value = bundle.get(field_name)
            if not isinstance(value, str) or value.strip() == "":
                return None
            normalized[field_name] = value.strip()
        return normalized

    @staticmethod
    def _extract_tool_name(tool_info: Any | None) -> str | None:
        """Extracts tool name from object or dict metadata.

        Args:
            tool_info: Candidate metadata object or dictionary.

        Returns:
            Extracted tool name string, or ``None`` when not found.
        """
        if tool_info is None:
            return None
        return AgentCoreToolMCPAdapter._extract_first_non_empty_str(
            tool_info,
            ("name", "tool_id", "tool_name"),
        )

    @staticmethod
    def _extract_server_name(mcp_info: Any | None) -> str | None:
        """Extracts MCP server name from object or dict metadata.

        Args:
            mcp_info: Candidate MCP metadata object or dictionary.

        Returns:
            Extracted server name string, or ``None`` when not found.
        """
        if mcp_info is None:
            return None
        return AgentCoreToolMCPAdapter._extract_first_non_empty_str(
            mcp_info,
            ("server_name", "server_id", "server"),
        )

    @staticmethod
    def _extract_capability_id(mcp_info: Any | None) -> str | None:
        """Extracts MCP capability identifier from object or dict metadata.

        Args:
            mcp_info: Candidate MCP metadata object or dictionary.

        Returns:
            Extracted capability id string, or ``None`` when not found.
        """
        if mcp_info is None:
            return None
        return AgentCoreToolMCPAdapter._extract_first_non_empty_str(
            mcp_info,
            ("capability_id", "name", "capability"),
        )

    @staticmethod
    def _extract_mcp_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        """Extracts nested MCP payload dict from action input JSON.

        Args:
            payload: Action input JSON dictionary, may be ``None``.

        Returns:
            Nested MCP payload dictionary, or ``None`` when absent.
        """
        if payload is None:
            return None
        raw = payload.get("mcp")
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _extract_capability_scope(payload: dict[str, Any] | None) -> list[str]:
        """Extracts and normalizes capability scope from action payload.

        Normalization strategy:
          - Accept list-like input (list/tuple/set) or a single scalar value.
          - Convert entries to strings and trim surrounding whitespace.
          - Drop empty entries after trim.
          - De-duplicate while preserving first-seen order.
        """
        if payload is None:
            return []
        raw = payload.get("capability_scope")
        if raw is None:
            # Compatibility path for older action payloads that provided a
            # single capability field instead of capability_scope.
            raw = payload.get("capability")

        if isinstance(raw, (list, tuple, set)):
            candidates = list(raw)
        elif raw is None:
            return []
        else:
            candidates = [raw]

        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item is None:
                continue
            token = str(item).strip()
            if not token or token in seen:
                continue
            normalized.append(token)
            seen.add(token)
        return normalized

    @staticmethod
    def _extract_optional_str(mcp_info: Any | None, key: str) -> str | None:
        """Extracts optional string fields from object or dict metadata.

        Args:
            mcp_info: Candidate metadata object or dictionary.
            key: Field key to extract.

        Returns:
            Extracted string value, or ``None`` when absent.
        """
        if mcp_info is None:
            return None
        return AgentCoreToolMCPAdapter._extract_first_non_empty_str(
            mcp_info,
            (key,),
        )

    @staticmethod
    def _extract_first_non_empty_str(
        source: Any | None,
        keys: tuple[str, ...],
    ) -> str | None:
        """Returns the first non-empty string-like value for candidate keys.

        The helper intentionally keeps key order significant so callers can
        preserve historical precedence for backward compatibility while adding
        new aliases.

        Args:
            source: Candidate metadata object or dictionary.
            keys: Ordered tuple of field keys to probe.

        Returns:
            First non-empty string value found, or ``None`` when all keys
            yield empty or absent values.
        """
        if source is None:
            return None

        if isinstance(source, dict):
            for key in keys:
                value = source.get(key)
                token = str(value).strip() if value is not None else ""
                if token:
                    return token
            return None

        for key in keys:
            value = getattr(source, key, None)
            token = str(value).strip() if value is not None else ""
            if token:
                return token
        return None
