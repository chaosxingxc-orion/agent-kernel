"""Session adapter for binding platform sessions to kernel run identifiers.

Design intent:
  - Maintain a minimal mapping between external ``session_id`` and kernel
    ``run_id`` values.
  - Translate platform callback envelopes into kernel ``SignalRunRequest``.

Architectural boundary:
  - This adapter does not decide lifecycle transitions.
  - It does not validate admission or policy.
  - It does not execute tools or write runtime events.

Rationale:
  - Session/callback semantics evolve frequently in platform frameworks.
  - Isolating translation here keeps kernel contracts stable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_kernel.kernel.contracts import SignalRunRequest


@dataclass(frozen=True, slots=True)
class AgentCoreCallbackInput:
    """Represents one platform callback that must be translated to a signal.

    Attributes:
        session_id: Platform session identifier for routing.
        callback_type: Callback type discriminator for signal mapping.
        callback_payload: Optional callback payload dictionary.
        run_id: Optional explicit run id for direct routing.
        caused_by: Optional causal reference for provenance tracing.
    """

    session_id: str
    callback_type: str
    callback_payload: dict[str, Any] | None = None
    run_id: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class _SessionRunBinding:
    """Represents one immutable session-to-run binding record.

    The adapter stores binding records instead of only run ids so that:
      - binding kind can be queried (primary/child/recovery),
      - dedup can be enforced per ``(run_id, binding_kind)`` pair,
      - bind-time ordering remains deterministic for compatibility behavior.
    """

    run_id: str
    binding_kind: str


class AgentCoreSessionAdapter:
    """Owns platform session-to-run binding and callback translation.

    Internal storage intentionally remains in-memory for the PoC phase. This is
    sufficient for contract verification and unit-level integration tests.
    """

    def __init__(self) -> None:
        # Storage model:
        #   session_id -> ordered binding records.
        # The list preserves bind sequence so legacy callers that expect
        # "latest bound run" semantics continue to work.
        self._session_bindings: dict[str, list[_SessionRunBinding]] = {}

    async def bind_run_to_session(
        self,
        session_id: str,
        run_id: str,
        binding_kind: str,
    ) -> None:
        """Binds one run to one platform session.

        Args:
            session_id: Platform session identifier.
            run_id: Kernel run identifier.
            binding_kind: Binding category such as primary,
                child, or recovery.

        Dedup policy:
            Same ``run_id`` + same ``binding_kind``: idempotent,
            do not append. Same ``run_id`` + different
            ``binding_kind``: allowed concurrently.
        """
        bindings = self._session_bindings.setdefault(session_id, [])
        candidate = _SessionRunBinding(run_id=run_id, binding_kind=binding_kind)
        if candidate not in bindings:
            bindings.append(candidate)

    async def bind_openjiuwen_session(
        self,
        session: Any,
        run_id: str,
        binding_kind: str = "primary",
    ) -> None:
        """Binds a run using an openjiuwen session object.

        Args:
            session: Session-like object exposing ``session_id()`` or
                ``session_id``.
            run_id: Kernel run identifier to bind.
            binding_kind: Optional binding category.

        Raises:
            ValueError: If session identity cannot be extracted.
        """
        session_id = self._extract_session_id(session)
        if not session_id:
            raise ValueError("session must provide session_id() or a session_id field.")
        await self.bind_run_to_session(session_id, run_id, binding_kind)

    async def resolve_session_run(self, session_id: str) -> list[str]:
        """Resolves all run identifiers bound to one session.

        Args:
            session_id: Platform session identifier.

        Returns:
            A list of bound run identifiers ordered by bind time across all
            binding kinds.

        Compatibility note:
            This method intentionally keeps the legacy "all bindings" view and
            does not filter by kind.
        """
        bindings = self._session_bindings.get(session_id, [])
        return [binding.run_id for binding in bindings]

    async def resolve_session_run_by_kind(self, session_id: str, binding_kind: str) -> list[str]:
        """Resolves run identifiers for one session filtered by binding kind.

        Args:
            session_id: Platform session identifier.
            binding_kind: Binding category to match exactly.

        Returns:
            Run identifiers bound under ``binding_kind``, ordered by bind time.
        """
        bindings = self._session_bindings.get(session_id, [])
        return [binding.run_id for binding in bindings if binding.binding_kind == binding_kind]

    async def resolve_openjiuwen_session(self, session: Any) -> list[str]:
        """Resolves all runs bound to one openjiuwen session object.

        Returns:
            Bound run ids. Empty list when session cannot be identified.

        Args:
            session: (description)
        """
        session_id = self._extract_session_id(session)
        if not session_id:
            return []
        return await self.resolve_session_run(session_id)

    def translate_callback(self, input_value: AgentCoreCallbackInput) -> SignalRunRequest:
        """Translates one platform callback into a kernel signal request.

        Args:
            input_value: Callback payload from the platform layer.

        Routing policy:
            1. Use explicit ``run_id`` if provided in callback input.
            2. Otherwise route to the latest run bound to ``session_id``.
            3. If no binding exists, conservatively route by ``session_id``
               itself to keep the signal path deterministic in PoC mode.

        Returns:
            A kernel signal request with deterministic routing behavior.
        """
        run_id = input_value.run_id
        if run_id is None:
            bindings = self._session_bindings.get(input_value.session_id, [])
            run_id = bindings[-1].run_id if bindings else input_value.session_id
        return SignalRunRequest(
            run_id=run_id,
            signal_type=input_value.callback_type,
            signal_payload=input_value.callback_payload,
            caused_by=input_value.caused_by,
        )

    def from_session_signal(self, input_value: AgentCoreCallbackInput) -> SignalRunRequest:
        """Ingress-compatible alias for translating session signal input.
        Args:
            input_value: (description)
        Returns:
            SignalRunRequest: (description)
        """
        return self.translate_callback(input_value)

    def from_callback(self, input_value: AgentCoreCallbackInput) -> SignalRunRequest:
        """Ingress-compatible alias for translating callback input.
        Args:
            input_value: (description)
        Returns:
            SignalRunRequest: (description)
        """
        return self.translate_callback(input_value)

    @staticmethod
    def _extract_session_id(session: Any | None) -> str | None:
        """Extracts ``session_id`` from flexible session representations.

        Supported forms:
          - raw string session id
          - object exposing callable ``session_id()``
          - object exposing string attribute ``session_id``

        Args:
            session: Candidate session value to extract from.

        Returns:
            Extracted session id string, or ``None`` when extraction fails.
        """
        if session is None:
            return None
        if isinstance(session, str):
            return session
        session_id_fn = getattr(session, "session_id", None)
        if callable(session_id_fn):
            return str(session_id_fn())
        session_id_attr = getattr(session, "session_id", None)
        if isinstance(session_id_attr, str):
            return session_id_attr
        return None
