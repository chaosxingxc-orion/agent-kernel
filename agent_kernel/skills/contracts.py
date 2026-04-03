"""Defines skill contracts for the agent_kernel execution layer.

The agent_kernel architecture treats skills as executor-governed
capability runtimes. They are intentionally excluded from lifecycle
authority, event authority, and recovery authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import (
        EffectClass,
        ExternalIdempotencyLevel,
    )

SkillFailureCode = Literal[
    "tool_error",
    "mcp_error",
    "model_error",
    "validation_error",
    "permission_denied",
    "quota_exceeded",
    "timeout",
    "unknown",
]
SkillRuntimeHost = Literal["cli_process", "in_process_python", "remote_service"]


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Defines one registered skill and its governance metadata.

    Attributes:
        skill_id: Unique skill identifier.
        version: Semantic version of the skill.
        skill_kind: Logical category of the skill.
        effect_class: Declared side-effect class for admission policy.
        input_schema_ref: Reference to the input JSON schema.
        output_schema_ref: Reference to the output JSON schema.
        display_name: Optional human-readable display name.
        description: Optional human-readable description.
        external_idempotency_level: Optional external idempotency guarantee.
        timeout_ms: Optional execution timeout in milliseconds.
        retryable: Optional retry eligibility flag.
        capability_scope: Ordered list of capability scope identifiers.
        tool_bindings: Ordered list of bound tool names.
        mcp_bindings: Ordered list of bound MCP server capabilities.
        metadata: Arbitrary metadata dictionary.
    """

    skill_id: str
    version: str
    skill_kind: str
    effect_class: EffectClass
    input_schema_ref: str
    output_schema_ref: str
    display_name: str | None = None
    description: str | None = None
    external_idempotency_level: ExternalIdempotencyLevel | None = None
    timeout_ms: int | None = None
    retryable: bool | None = None
    capability_scope: list[str] = field(default_factory=list)
    tool_bindings: list[str] = field(default_factory=list)
    mcp_bindings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillRequest:
    """Carries one executor-managed invocation into a skill runtime.

    Attributes:
        run_id: Kernel run identifier.
        action_id: Action identifier triggering the skill invocation.
        skill_id: Target skill identifier.
        skill_version: Optional skill version constraint.
        input_ref: Optional input reference string.
        input_json: Optional input payload dictionary.
        context_ref: Optional context binding reference.
        grant_ref: Optional admission grant reference.
        caused_by: Optional causal reference for provenance tracing.
    """

    run_id: str
    action_id: str
    skill_id: str
    skill_version: str | None = None
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    context_ref: str | None = None
    grant_ref: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class SkillObservation:
    """Carries non-authoritative local observations returned by a skill.

    Attributes:
        observation_type: Discriminator for the observation category.
        payload_ref: Optional reference to stored observation payload.
        payload_json: Optional inline observation payload dictionary.
    """

    observation_type: str
    payload_ref: str | None = None
    payload_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SkillResult:
    """Carries the only allowed outputs a skill may return to the executor.

    Attributes:
        skill_id: Skill that produced this result.
        action_id: Action that triggered the skill execution.
        success: Whether the skill execution succeeded.
        output_ref: Optional reference to stored output payload.
        output_json: Optional inline output payload dictionary.
        local_observations: Ordered list of non-authoritative observations.
        evidence_ref: Optional reference to execution evidence.
        failure_code: Optional failure code when ``success`` is False.
        failure_detail: Optional human-readable failure description.
    """

    skill_id: str
    action_id: str
    success: bool
    output_ref: str | None = None
    output_json: dict[str, Any] | None = None
    local_observations: list[SkillObservation] = field(default_factory=list)
    evidence_ref: str | None = None
    failure_code: SkillFailureCode | None = None
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class SkillExecutionInput:
    """Carries the executor-level request to run a skill-backed action.

    Attributes:
        action_id: Action identifier for the execution request.
        run_id: Kernel run identifier.
        action_type: Discriminator for the action type.
        input_ref: Optional input reference string.
        input_json: Optional input payload dictionary.
        context_ref: Optional context binding reference.
        preferred_skill_id: Optional preferred skill identifier.
        preferred_skill_version: Optional preferred skill version.
        grant_ref: Optional admission grant reference.
    """

    action_id: str
    run_id: str
    action_type: str
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    context_ref: str | None = None
    preferred_skill_id: str | None = None
    preferred_skill_version: str | None = None
    grant_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SkillExecutionResult:
    """Carries the resolved skill metadata and the runtime result.

    Attributes:
        skill: Resolved skill definition that governed execution.
        result: Skill execution result returned by the runtime.
    """

    skill: SkillDefinition
    result: SkillResult


@dataclass(frozen=True, slots=True)
class ResolvedSkillPlan:
    """Carries resolved runtime execution plan for one skill invocation."""

    skill: SkillDefinition
    host_kind: SkillRuntimeHost
    grant_ref: str | None = None
    capability_snapshot_ref: str | None = None
    capability_snapshot_hash: str | None = None
    idempotency_envelope: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SkillActionResolveInput:
    """Carries the information required to resolve a skill for one action."""

    action_type: str
    run_id: str
    policy_tags: list[str] = field(default_factory=list)
    preferred_skill_id: str | None = None
    preferred_version: str | None = None


class SkillRegistry(Protocol):
    """Owns skill metadata and versioned bindings."""

    async def get(self, skill_id: str, version: str | None = None) -> SkillDefinition | None:
        """Retrieves one skill definition by id and optional version.

        Args:
            skill_id: Skill identifier to look up.
            version: Optional version constraint.

        Returns:
            Matching skill definition, or ``None`` if not found.
        """
        ...

    async def resolve_by_action(self, action_type: str) -> SkillDefinition | None:
        """Resolves one skill definition by action type.

        Args:
            action_type: Action type discriminator.

        Returns:
            Matching skill definition, or ``None`` if not found.
        """
        ...

    async def list_by_kind(self, skill_kind: str) -> list[SkillDefinition]:
        """Lists all registered skills matching a given kind.

        Args:
            skill_kind: Skill kind discriminator.

        Returns:
            List of matching skill definitions.
        """
        ...


class SkillResolver(Protocol):
    """Resolves which registered skill should execute one action."""

    async def resolve(
        self,
        action: SkillActionResolveInput,
    ) -> SkillDefinition:
        """Resolves which registered skill should execute one action.

        Args:
            action: Action resolve input with type and preference hints.

        Returns:
            Resolved skill definition for the action.
        """
        ...


class SkillRuntime(Protocol):
    """Executes one skill request under executor governance."""

    async def execute(self, request: SkillRequest) -> SkillResult:
        """Executes one skill request and returns the result.

        Args:
            request: Skill execution request with action and input payload.

        Returns:
            Skill execution result with output or failure details.
        """
        ...


class ManagedSkillRuntime(SkillRuntime, Protocol):
    """Extends skill runtime with optional managed lifecycle hooks."""

    async def validate(self, request: SkillRequest) -> None:
        """Validates one request before execute.
        Args:
            request: (description)
        """
        ...

    async def warmup(self) -> None:
        """Warms up runtime resources."""
        ...

    async def shutdown(self) -> None:
        """Releases runtime resources."""
        ...


class SkillRuntimeFactory(Protocol):
    """Creates skill runtime instances from registered definitions."""

    async def create(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates one skill runtime from a registered definition.

        Args:
            definition: Skill definition governing the runtime instance.

        Returns:
            Initialized skill runtime ready for execution.
        """
        ...


class SkillRuntimeHostFactory(Protocol):
    """Creates runtimes by explicit host kind routing."""

    async def create_for_host(
        self,
        definition: SkillDefinition,
        host_kind: SkillRuntimeHost,
    ) -> SkillRuntime:
        """Creates runtime for a concrete host kind.

        Args:
            definition: Skill definition governing the runtime instance.
            host_kind: Target host kind to create the runtime for.

        Returns:
            Initialized skill runtime for the requested host kind.
        """
        ...


class LocalSkillRuntimeFactory(Protocol):
    """Creates local-host skill runtimes."""

    async def create_cli_process(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime for ``cli_process`` host.
        Args:
            definition: (description)
        Returns:
            SkillRuntime: (description)
        """
        ...

    async def create_in_process_python(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime for ``in_process_python`` host.
        Args:
            definition: (description)
        Returns:
            SkillRuntime: (description)
        """
        ...


class RemoteSkillGatewayFactory(Protocol):
    """Creates remote-service skill gateway runtimes."""

    async def create_remote_service(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime for ``remote_service`` host.

        Args:
            definition: Skill definition governing the runtime instance.

        Returns:
            Initialized remote-service skill runtime.
        """
        ...
