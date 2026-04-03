"""Recovery mode registry for agent-kernel.

Provides ``KERNEL_RECOVERY_MODE_REGISTRY``, a central catalog that maps
planner actions to kernel recovery mode strings.  Teams extending the kernel
can register custom modes at module import time without modifying gate.py.

Usage::

    from agent_kernel.kernel.recovery.mode_registry import KERNEL_RECOVERY_MODE_REGISTRY

    # Register a custom mode at application startup
    KERNEL_RECOVERY_MODE_REGISTRY.register("auto_rollback", "my_custom_rollback")

    # The gate will now resolve 'auto_rollback' planner actions to the
    # 'my_custom_rollback' recovery mode string.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_registry_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoveryModeRegistry:
    """Maps planner action strings to kernel recovery mode strings.

    Raises ``ValueError`` on duplicate registration to prevent accidental
    shadowing.  Teams extending the kernel should call ``register()`` at
    module import time, not at request time.
    """

    _entries: dict[str, str] = field(default_factory=dict)

    def register(self, plan_action: str, mode: str) -> None:
        """Register a ``plan_action`` → ``mode`` mapping.

        Args:
            plan_action: The planner action string (e.g.
                ``"schedule_compensation"``).
            mode: The recovery mode string to emit in ``RecoveryDecision``
                (e.g. ``"static_compensation"``).

        Raises:
            ValueError: When ``plan_action`` is already registered.
        """
        if plan_action in self._entries:
            raise ValueError(
                f"Recovery plan action '{plan_action}' is already registered "
                f"with mode '{self._entries[plan_action]}'. "
                "Use a unique plan_action or update the existing entry."
            )
        self._entries[plan_action] = mode

    def get(self, plan_action: str) -> str | None:
        """Return the mode string for *plan_action*, or ``None`` when unknown.

        Args:
            plan_action: The planner action string to look up.

        Returns:
            Registered mode string, or ``None`` when not found.
        """
        return self._entries.get(plan_action)

    def known_actions(self) -> frozenset[str]:
        """Return all registered plan action strings.

        Returns:
            Immutable set of registered plan action strings.
        """
        return frozenset(self._entries)


# ---------------------------------------------------------------------------
# Kernel-built-in recovery mode registry
# ---------------------------------------------------------------------------

KERNEL_RECOVERY_MODE_REGISTRY: RecoveryModeRegistry = RecoveryModeRegistry()

# Built-in mappings: planner action → recovery mode.
_BUILTIN_MAPPINGS: list[tuple[str, str]] = [
    ("schedule_compensation", "static_compensation"),
    ("notify_human_operator", "human_escalation"),
    ("abort_run", "abort"),
]

for _action, _mode in _BUILTIN_MAPPINGS:
    KERNEL_RECOVERY_MODE_REGISTRY.register(_action, _mode)
