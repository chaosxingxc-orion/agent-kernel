"""ScriptRuntime routing registry (R4a).

Replaces ad-hoc host_kind if/elif chains with a registry-driven dispatch
that allows third-party runtimes to be injected at startup.

Usage::

    from agent_kernel.kernel.cognitive.script_runtime_registry import (
        KERNEL_SCRIPT_RUNTIME_REGISTRY,
    )

    # Dispatch to the correct runtime:
    result = await KERNEL_SCRIPT_RUNTIME_REGISTRY.dispatch(script_input)

    # Register a custom runtime:
    KERNEL_SCRIPT_RUNTIME_REGISTRY.register(
        "my_host_kind", my_runtime_instance, description="Custom executor"
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import ScriptActivityInput, ScriptResult

# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScriptRuntimeDescriptor:
    """Metadata for a registered ScriptRuntime host_kind."""

    host_kind: str
    description: str
    is_safe_for_production: bool = False
    supports_timeout: bool = True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ScriptRuntimeRegistry:
    """Maps host_kind strings to ScriptRuntime instances.

    Built-in runtimes are registered by ``_register_builtin_runtimes()``.
    Third-party code may call ``register()`` at application startup to add
    custom host_kinds without modifying kernel source.
    """

    def __init__(self) -> None:
        """Initialize the instance with configured dependencies."""
        self._runtimes: dict[str, Any] = {}
        self._descriptors: dict[str, ScriptRuntimeDescriptor] = {}
        self._production_mode: bool = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        host_kind: str,
        runtime: Any,
        *,
        description: str = "",
        is_safe_for_production: bool = False,
        supports_timeout: bool = True,
    ) -> None:
        """Register a ScriptRuntime for *host_kind*.

        Overwrites any existing registration for the same host_kind.

        Args:
            host_kind: The ``ScriptActivityInput.host_kind`` value to match.
            runtime: Any object with an ``execute_script(input) -> ScriptResult`` coroutine.
            description: Human-readable description (for logging/debugging).
            is_safe_for_production: False for PoC/test runtimes.
            supports_timeout: Whether the runtime enforces ``timeout_ms``.

        """
        self._runtimes[host_kind] = runtime
        self._descriptors[host_kind] = ScriptRuntimeDescriptor(
            host_kind=host_kind,
            description=description,
            is_safe_for_production=is_safe_for_production,
            supports_timeout=supports_timeout,
        )

    def enable_production_mode(self) -> None:
        """Enable production mode, blocking dispatch of unsafe runtimes.

        Once called, any ``dispatch()`` call targeting a runtime whose
        descriptor has ``is_safe_for_production=False`` will raise
        ``RuntimeError``.  This method is idempotent.
        """
        self._production_mode = True

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, host_kind: str) -> Any | None:
        """Return the registered runtime for *host_kind*, or ``None``.

        Args:
            host_kind: Skill runtime host key to look up.

        Returns:
            Registered runtime instance, or ``None`` when not found.

        """
        return self._runtimes.get(host_kind)

    def get_descriptor(self, host_kind: str) -> ScriptRuntimeDescriptor | None:
        """Return the descriptor for *host_kind*, or ``None``.

        Args:
            host_kind: Skill runtime host key to look up.

        Returns:
            Host descriptor, or ``None`` when not found.

        """
        return self._descriptors.get(host_kind)

    def known_host_kinds(self) -> list[str]:
        """Return all registered host_kind strings.

        Returns:
            list[str]:

        """
        return list(self._runtimes.keys())

    def all_descriptors(self) -> list[ScriptRuntimeDescriptor]:
        """Return all registered descriptors.

        Returns:
            list[ScriptRuntimeDescriptor]:

        """
        return list(self._descriptors.values())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Route *input_value* to the correct runtime and execute it.

        Args:
            input_value: Script execution payload (contains ``host_kind``).

        Returns:
            ScriptResult from the matched runtime.

        Raises:
            KeyError: If no runtime is registered for ``input_value.host_kind``.

        """
        runtime = self._runtimes.get(input_value.host_kind)
        if runtime is None:
            registered = ", ".join(sorted(self._runtimes)) or "<none>"
            raise KeyError(
                f"No ScriptRuntime registered for host_kind={input_value.host_kind!r}. "
                f"Registered: {registered}"
            )
        descriptor = self._descriptors.get(input_value.host_kind)
        if (
            self._production_mode
            and descriptor is not None
            and not descriptor.is_safe_for_production
        ):
            raise RuntimeError(
                f"ScriptRuntime {input_value.host_kind!r} is not production-safe; "
                "cannot dispatch in production mode"
            )
        return await runtime.execute_script(input_value)


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


def _register_builtin_runtimes(registry: ScriptRuntimeRegistry) -> None:
    """Populate *registry* with the three built-in runtimes."""
    # Lazy import to avoid circular dependency at module load time.
    from agent_kernel.kernel.cognitive.script_runtime import (
        EchoScriptRuntime,
        InProcessPythonScriptRuntime,
        LocalProcessScriptRuntime,
    )

    registry.register(
        "echo",
        EchoScriptRuntime(),
        description="PoC stub that echoes parameters as JSON. Not for production.",
        is_safe_for_production=False,
        supports_timeout=False,
    )
    registry.register(
        "in_process_python",
        InProcessPythonScriptRuntime(),
        description="exec()-based in-process Python runtime. PoC/test only; NOT production-safe.",
        is_safe_for_production=False,
        supports_timeout=True,
    )
    registry.register(
        "local_process",
        LocalProcessScriptRuntime(),
        description="asyncio subprocess runtime for local shell scripts.",
        is_safe_for_production=True,
        supports_timeout=True,
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

#: Kernel-level singleton registry.  Import and use directly.
KERNEL_SCRIPT_RUNTIME_REGISTRY = ScriptRuntimeRegistry()
_register_builtin_runtimes(KERNEL_SCRIPT_RUNTIME_REGISTRY)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def configure_local_process_timeout(timeout_ms: int) -> None:
    """Replace the ``"local_process"`` registration with the given timeout.

    Must be called at application startup (before any dispatch) to wire
    ``KernelConfig.script_timeout_s`` into the singleton registry.

    Args:
        timeout_ms: Timeout in milliseconds forwarded to
            ``LocalProcessScriptRuntime(default_timeout_ms=timeout_ms)``.

    """
    from agent_kernel.kernel.cognitive.script_runtime import LocalProcessScriptRuntime

    KERNEL_SCRIPT_RUNTIME_REGISTRY.register(
        "local_process",
        LocalProcessScriptRuntime(default_timeout_ms=timeout_ms),
        description="asyncio subprocess runtime for local shell scripts.",
        is_safe_for_production=True,
        supports_timeout=True,
    )


# ---------------------------------------------------------------------------
# Convenience validator
# ---------------------------------------------------------------------------


def validate_host_kind(host_kind: str, *, strict: bool = False) -> bool:
    """Return True if *host_kind* is registered in ``KERNEL_SCRIPT_RUNTIME_REGISTRY``.

    Args:
        host_kind: The host_kind string to check.
        strict: If True, raises ``ValueError`` for unknown host_kinds.

    Returns:
        True if known, False otherwise (unless *strict* raises first).

    Raises:
            Exception:

    """
    if host_kind in KERNEL_SCRIPT_RUNTIME_REGISTRY.known_host_kinds():
        return True
    known = KERNEL_SCRIPT_RUNTIME_REGISTRY.known_host_kinds()
    msg = f"Unknown host_kind={host_kind!r}. Known: {known}"
    if strict:
        raise ValueError(msg)
    return False
