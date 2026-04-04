"""Action type registry for agent-kernel.

Provides ``KERNEL_ACTION_TYPE_REGISTRY``, a central catalog that maps
``action_type`` discriminator strings to ``ActionTypeDescriptor`` metadata.
Teams extending the kernel can register custom action types at module import
time without modifying TurnEngine or ExecutorService core code.

Usage::

    from agent_kernel.kernel.action_type_registry import KERNEL_ACTION_TYPE_REGISTRY

    descriptor = KERNEL_ACTION_TYPE_REGISTRY.get("tool_call")
    assert descriptor is not None
    print(descriptor.description)

    # Register a custom action type at application startup:
    KERNEL_ACTION_TYPE_REGISTRY.register(ActionTypeDescriptor(
        action_type="custom_rpc",
        description="Custom RPC action dispatched via gRPC executor.",
        executor_hint="remote_service",
        is_idempotent=False,
    ))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_registry_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionTypeDescriptor:
    """Describes one registered ``action_type`` discriminator.

    Attributes:
        action_type: Canonical discriminator string (e.g. ``"tool_call"``).
        description: Human-readable summary of what this action type does.
        executor_hint: Suggested executor routing hint for this action type
            (e.g. ``"local_process"``, ``"remote_service"``).
            Executors may use this for optimised routing; non-binding.
        is_idempotent: Whether dispatches of this action type are safe to
            retry without side effects.  Used by RetryingExecutorService and
            circuit breaker heuristics.
        supports_dedupe: Whether this action type participates in
            ``DedupeStore`` at-most-once tracking.  Set ``False`` for
            fire-and-forget or read-only actions.
        allowed_effect_classes: Optional explicit whitelist of ``effect_class``
            values permitted for actions of this type.  When non-empty, callers
            can use ``validate_effect_class()`` to reject unknown classes before
            dispatch.  When empty (default), no per-type restriction is applied.
    """

    action_type: str
    description: str
    executor_hint: str = "local_process"
    is_idempotent: bool = True
    supports_dedupe: bool = True
    allowed_effect_classes: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class ActionTypeRegistry:
    """Central registry of known ``action_type`` discriminator strings.

    Raises ``ValueError`` on duplicate registration to prevent accidental
    shadowing.  Teams extending the kernel should call ``register()`` at
    module import time, not at request time.
    """

    _entries: dict[str, ActionTypeDescriptor] = field(default_factory=dict)

    def register(self, descriptor: ActionTypeDescriptor) -> None:
        """Register a new action type descriptor.

        Args:
            descriptor: The descriptor to register.

        Raises:
            ValueError: When ``descriptor.action_type`` is already registered.
        """
        if descriptor.action_type in self._entries:
            raise ValueError(
                f"Action type '{descriptor.action_type}' is already registered. "
                "Use a unique action_type string or update the existing entry."
            )
        self._entries[descriptor.action_type] = descriptor

    def get(self, action_type: str) -> ActionTypeDescriptor | None:
        """Return the descriptor for *action_type*, or ``None`` when unknown.

        Args:
            action_type: The discriminator string to look up.

        Returns:
            Matching descriptor, or ``None`` when not registered.
        """
        return self._entries.get(action_type)

    def known_types(self) -> frozenset[str]:
        """Return all registered action type strings.

        Returns:
            Immutable set of registered action type strings.
        """
        return frozenset(self._entries)

    def all(self) -> list[ActionTypeDescriptor]:
        """Return all registered descriptors sorted by action_type.

        Returns:
            Alphabetically sorted list of all registered descriptors.
        """
        return sorted(self._entries.values(), key=lambda d: d.action_type)


# ---------------------------------------------------------------------------
# Kernel-built-in action type registry
# ---------------------------------------------------------------------------

KERNEL_ACTION_TYPE_REGISTRY: ActionTypeRegistry = ActionTypeRegistry()

_KERNEL_ACTION_TYPES: list[ActionTypeDescriptor] = [
    ActionTypeDescriptor(
        action_type="tool_call",
        description=(
            "Call a tool function registered in the agent's tool bindings. "
            "Typically dispatched in-process or via a local MCP server."
        ),
        executor_hint="local_process",
        is_idempotent=False,
        supports_dedupe=True,
    ),
    ActionTypeDescriptor(
        action_type="mcp_call",
        description=(
            "Call a Model Context Protocol (MCP) server endpoint. "
            "Dispatched via MCP transport to a remote or local MCP server."
        ),
        executor_hint="local_process",
        is_idempotent=False,
        supports_dedupe=True,
    ),
    ActionTypeDescriptor(
        action_type="skill_script",
        description=(
            "Execute a registered skill script via ScriptRuntime. "
            "Scripts may be in-process Python or spawned as a subprocess."
        ),
        executor_hint="local_process",
        is_idempotent=False,
        supports_dedupe=True,
    ),
    ActionTypeDescriptor(
        action_type="sub_agent",
        description=(
            "Spawn a child agent run for a sub-task. "
            "Dispatched via TemporalGateway.start_child_run()."
        ),
        executor_hint="remote_service",
        is_idempotent=True,
        supports_dedupe=True,
    ),
    ActionTypeDescriptor(
        action_type="noop",
        description=(
            "Explicit no-operation marker. "
            "TurnEngine returns completed_noop without calling the executor."
        ),
        executor_hint="local_process",
        is_idempotent=True,
        supports_dedupe=False,
    ),
]

for _descriptor in _KERNEL_ACTION_TYPES:
    KERNEL_ACTION_TYPE_REGISTRY.register(_descriptor)

# ---------------------------------------------------------------------------
# Kernel-built-in effect_class vocabulary
# ---------------------------------------------------------------------------

#: Canonical ``effect_class`` values understood by the kernel's admission and
#: recovery layers.  Extensions may add custom classes, but using unknown
#: values without registering them triggers a WARNING from
#: ``validate_effect_class()``.
KNOWN_EFFECT_CLASSES: frozenset[str] = frozenset(
    {
        "read_only",
        "compensatable_write",
        "fire_forget",
        "irreversible_write",
    }
)


def validate_effect_class(effect_class: str, strict: bool = False) -> bool:
    """Check whether *effect_class* is in the canonical ``KNOWN_EFFECT_CLASSES`` set.

    Unknown effect classes are not inherently illegal — teams may extend the
    vocabulary — but unknown values bypass admission heuristics and circuit
    breaker routing.  Call with ``strict=True`` in deployments that want to
    prevent ad-hoc class pollution.

    Args:
        effect_class: The effect class string to validate (e.g. ``"read_only"``).
        strict: When ``True`` raises ``ValueError`` for unknown classes.
            When ``False`` (default) only emits a WARNING log.

    Returns:
        ``True`` when the effect_class is in ``KNOWN_EFFECT_CLASSES``.

    Raises:
        ValueError: When ``strict=True`` and the effect_class is unknown.
    """
    if effect_class in KNOWN_EFFECT_CLASSES:
        return True
    msg = (
        f"Effect class '{effect_class}' is not in KNOWN_EFFECT_CLASSES "
        f"({sorted(KNOWN_EFFECT_CLASSES)}). Add it to KNOWN_EFFECT_CLASSES or "
        "accept reduced admission heuristics for this class."
    )
    if strict:
        raise ValueError(msg)
    _registry_logger.warning(msg)
    return False


def validate_action_type(action_type: str, strict: bool = False) -> bool:
    """Check whether *action_type* is registered in ``KERNEL_ACTION_TYPE_REGISTRY``.

    Args:
        action_type: The discriminator string to validate.
        strict: When ``True`` raises ``ValueError`` for unknown types so that
            strict-mode deployments prevent ad-hoc action type pollution.
            When ``False`` (default) only emits a WARNING log.

    Returns:
        ``True`` when the action_type is registered.

    Raises:
        ValueError: When ``strict=True`` and the action_type is not registered.
    """
    if action_type in KERNEL_ACTION_TYPE_REGISTRY.known_types():
        return True
    msg = (
        f"Action type '{action_type}' is not registered in KERNEL_ACTION_TYPE_REGISTRY. "
        "Call KERNEL_ACTION_TYPE_REGISTRY.register() at module import time to prevent "
        "semantic pollution across teams."
    )
    if strict:
        raise ValueError(msg)
    _registry_logger.warning(msg)
    return False
