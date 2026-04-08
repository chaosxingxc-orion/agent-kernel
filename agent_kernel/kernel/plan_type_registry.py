"""Plan type registry for agent-kernel.

Provides ``KERNEL_PLAN_TYPE_REGISTRY``, a central catalog that maps
``plan_type`` discriminator strings to ``PlanTypeDescriptor`` metadata.
Teams extending the kernel can register custom plan types at module import
time without modifying PlanExecutor core code.

Usage::

    from agent_kernel.kernel.plan_type_registry import KERNEL_PLAN_TYPE_REGISTRY

    descriptor = KERNEL_PLAN_TYPE_REGISTRY.get("conditional")
    assert descriptor is not None
    print(descriptor.description)

    # Register a custom plan type at application startup:
    KERNEL_PLAN_TYPE_REGISTRY.register(PlanTypeDescriptor(
        plan_type="event_sourced_saga",
        description="Long-running saga coordinated via domain events.",
        is_speculative=False,
        requires_child_workflow=True,
    ))
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field

_registry_logger = logging.getLogger(__name__)

warnings.warn(
    "agent_kernel.kernel.plan_type_registry is deprecated. "
    "Plan types have moved to hi_agent.task_mgmt.plan_types. "
    "This module will be removed in the next major version.",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass(frozen=True, slots=True)
class PlanTypeDescriptor:
    """Describes one registered ``plan_type`` discriminator.

    Attributes:
        plan_type: Canonical discriminator string (e.g. ``"conditional"``).
        description: Human-readable summary of when to use this plan type.
        is_speculative: Whether this plan type runs in speculative mode.
            Speculative plans gate ``compensatable_write`` and
            ``irreversible_write`` side-effects until a winner is committed.
        requires_child_workflow: Whether execution requires spawning Child
            Workflows rather than in-process asyncio tasks.  Critical for
            Temporal History safety (avoids bloat in the parent Workflow).
        since_version: Kernel version when this plan type was introduced.

    """

    plan_type: str
    description: str
    is_speculative: bool = False
    requires_child_workflow: bool = False
    since_version: str = "0.2"


@dataclass(slots=True)
class PlanTypeRegistry:
    """Central registry of known ``plan_type`` discriminator strings.

    Raises ``ValueError`` on duplicate registration to prevent accidental
    shadowing.  Teams extending the kernel should call ``register()`` at
    module import time, not at request time.
    """

    _entries: dict[str, PlanTypeDescriptor] = field(default_factory=dict)

    def register(self, descriptor: PlanTypeDescriptor) -> None:
        """Register a new plan type descriptor.

        Args:
            descriptor: The descriptor to register.

        Raises:
            ValueError: When ``descriptor.plan_type`` is already registered.

        """
        if descriptor.plan_type in self._entries:
            raise ValueError(
                f"Plan type '{descriptor.plan_type}' is already registered. "
                "Use a unique plan_type string or update the existing entry."
            )
        self._entries[descriptor.plan_type] = descriptor

    def get(self, plan_type: str) -> PlanTypeDescriptor | None:
        """Return the descriptor for *plan_type*, or ``None`` when unknown.

        Args:
            plan_type: The discriminator string to look up.

        Returns:
            Matching descriptor, or ``None`` when not registered.

        """
        return self._entries.get(plan_type)

    def known_types(self) -> frozenset[str]:
        """Return all registered plan type strings.

        Returns:
            Immutable set of registered plan type strings.

        """
        return frozenset(self._entries)

    def all(self) -> list[PlanTypeDescriptor]:
        """Return all registered descriptors sorted by plan_type.

        Returns:
            Alphabetically sorted list of all registered descriptors.

        """
        return sorted(self._entries.values(), key=lambda d: d.plan_type)


# ---------------------------------------------------------------------------
# Kernel-built-in plan type registry
# ---------------------------------------------------------------------------

KERNEL_PLAN_TYPE_REGISTRY: PlanTypeRegistry = PlanTypeRegistry()

_KERNEL_PLAN_TYPES: list[PlanTypeDescriptor] = [
    PlanTypeDescriptor(
        plan_type="sequential",
        description=(
            "Ordered list of Actions executed one at a time. "
            "Each Action is a separate TurnEngine turn."
        ),
        is_speculative=False,
        requires_child_workflow=False,
        since_version="0.1",
    ),
    PlanTypeDescriptor(
        plan_type="parallel",
        description=(
            "Parallel groups of Actions with configurable join strategies. "
            "Groups execute sequentially; Actions within each group execute "
            "concurrently via asyncio.TaskGroup."
        ),
        is_speculative=False,
        requires_child_workflow=False,
        since_version="0.1",
    ),
    PlanTypeDescriptor(
        plan_type="conditional",
        description=(
            "Routes execution to a sub-plan based on the outcome of a gating "
            "Action. Enables workflow-style conditional branching within a run."
        ),
        is_speculative=False,
        requires_child_workflow=False,
        since_version="0.2",
    ),
    PlanTypeDescriptor(
        plan_type="dependency_graph",
        description=(
            "Directed acyclic graph of Actions with explicit node-level "
            "dependencies. Nodes execute in topological order; nodes at the "
            "same level execute concurrently. Uses graphlib.TopologicalSorter "
            "with canonical node_id ordering for Temporal determinism."
        ),
        is_speculative=False,
        requires_child_workflow=False,
        since_version="0.2",
    ),
    PlanTypeDescriptor(
        plan_type="speculative",
        description=(
            "Multiple candidate plans executed as parallel Child Workflows. "
            "Side-effects are gated until the platform signals which candidate "
            "won via commit_speculation. Losing candidates are cancelled. "
            "Child Workflow pattern avoids Temporal History bloat."
        ),
        is_speculative=True,
        requires_child_workflow=True,
        since_version="0.2",
    ),
]

for _descriptor in _KERNEL_PLAN_TYPES:
    KERNEL_PLAN_TYPE_REGISTRY.register(_descriptor)
