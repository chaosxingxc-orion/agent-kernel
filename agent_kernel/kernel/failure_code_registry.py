"""Pluggable failure-code → recovery-action registry.

Provides a framework for mapping TraceFailureCode to recommended recovery
actions and human gate types.  The kernel ships with an EMPTY default
registry — concrete mappings are provided by the platform layer (e.g. hi-agent).
"""

from agent_kernel.kernel.contracts import TraceFailureCode


class FailureCodeRegistry:
    """Maps failure codes to recovery actions and gate types.

    Platform layers register their own mappings at startup.
    The kernel provides the framework; business semantics stay upstream.
    """

    def __init__(self) -> None:
        self._recovery_map: dict[TraceFailureCode, str] = {}
        self._gate_map: dict[TraceFailureCode, str | None] = {}

    def register_recovery(self, code: TraceFailureCode, action: str) -> None:
        """Register a recovery action for a failure code."""
        self._recovery_map[code] = action

    def register_gate(self, code: TraceFailureCode, gate_type: str | None) -> None:
        """Register a human gate type for a failure code."""
        self._gate_map[code] = gate_type

    def register_batch(
        self,
        recovery: dict[TraceFailureCode, str] | None = None,
        gates: dict[TraceFailureCode, str | None] | None = None,
    ) -> None:
        """Register multiple mappings at once."""
        if recovery:
            self._recovery_map.update(recovery)
        if gates:
            self._gate_map.update(gates)

    def get_recovery(self, code: TraceFailureCode) -> str | None:
        """Look up recommended recovery action. Returns None if unmapped."""
        return self._recovery_map.get(code)

    def get_gate(self, code: TraceFailureCode) -> str | None:
        """Look up human gate type. Returns None if unmapped or no gate needed."""
        return self._gate_map.get(code)

    def has_mapping(self, code: TraceFailureCode) -> bool:
        """Check if a failure code has any mapping registered."""
        return code in self._recovery_map or code in self._gate_map


# Module-level default instance. Platform layers call .register_batch() at startup.
DEFAULT_FAILURE_CODE_REGISTRY = FailureCodeRegistry()
