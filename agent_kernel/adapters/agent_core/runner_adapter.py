"""Runner adapter between openjiuwen agent-core calls and kernel inputs.

Design intent:
  - This module is a *pure translation layer* in the architecture.
  - It maps external runner-facing arguments into kernel contract
    DTOs.
  - It never mutates authoritative kernel state and never triggers
    side effects.

Architectural boundary:
  - Lifecycle authority remains in the kernel
    (``RunActor``/workflow).
  - Admission, execution, and recovery remain kernel
    responsibilities.
  - This adapter only standardizes request shape at the boundary.

Why this matters:
  - Keeping adapters side-effect-free prevents "second authority
    centers" from emerging outside the kernel.
  - Strongly typed DTO mapping makes integration with openjiuwen
    evolvable without polluting kernel semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_kernel.kernel.contracts import (
    SpawnChildRunRequest,
    StartRunRequest,
)


@dataclass(frozen=True, slots=True)
class AgentCoreRunnerStartInput:
    """Represents one runner-originated start request from agent-core."""

    runner_kind: str
    session_id: str | None = None
    goal_ref: str | None = None
    goal_json: dict[str, Any] | None = None
    context_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AgentCoreChildSpawnInput:
    """Represents one child-run spawn request from agent-core.

    Attributes:
        parent_run_id: Parent run identifier for child lineage.
        runner_kind: Logical child run category identifier.
        child_goal_ref: Optional child goal reference string.
        child_goal_json: Optional child goal payload dictionary.

    """

    parent_run_id: str
    runner_kind: str
    child_goal_ref: str | None = None
    child_goal_json: dict[str, Any] | None = None


class AgentCoreRunnerAdapter:
    """Maps runner-centric platform requests into kernel requests.

    This adapter intentionally performs no policy checks. Any security,
    admission, idempotency, or lifecycle validation must happen inside kernel
    services after mapping is complete.
    """

    def from_openjiuwen_run_call(
        self,
        runner_kind: str,
        inputs: dict[str, Any] | None = None,
        session: Any | None = None,
        context_ref: str | None = None,
    ) -> StartRunRequest:
        """Convert an openjiuwen-style run call into ``StartRunRequest``.

        This helper aligns with common openjiuwen APIs where callers provide
        ``runner_kind + inputs + session``. The method normalizes those values
        into the kernel's external entry contract.

        Args:
            runner_kind: Logical run category from agent-core caller context.
            inputs: Raw user/task payload to pass as run input JSON.
            session: Optional session object or string identifier.
            context_ref: Optional context reference persisted by platform.

        Returns:
            A kernel ``StartRunRequest`` ready for facade submission.

        """
        return self.from_runner_start(
            AgentCoreRunnerStartInput(
                runner_kind=runner_kind,
                session_id=self._extract_session_id(session),
                goal_json=inputs,
                context_ref=context_ref,
            )
        )

    def from_openjiuwen_child_run_call(
        self,
        runner_kind: str,
        child_inputs: dict[str, Any] | None = None,
        parent_session: Any | None = None,
    ) -> SpawnChildRunRequest:
        """Convert a child workflow/agent call into ``SpawnChildRunRequest``.

        Parent run identity extraction prefers ``workflow_id`` when available
        because child runs are usually scoped by workflow lineage. If not
        available, it falls back to ``session_id``.

        Args:
            runner_kind: Child runner category.
            child_inputs: Input payload for the child run.
            parent_session: Session/workflow object from agent-core.

        Returns:
            A normalized child-run request.

        Raises:
            ValueError: If parent identity cannot be extracted from input.

        """
        parent_run_id = self._extract_workflow_id(parent_session) or self._extract_session_id(
            parent_session
        )
        if not parent_run_id:
            raise ValueError("parent_session must provide workflow_id() or session_id().")
        return self.from_runner_child_spawn(
            AgentCoreChildSpawnInput(
                parent_run_id=parent_run_id,
                runner_kind=runner_kind,
                child_goal_json=child_inputs,
            )
        )

    def from_runner_start(self, input_value: AgentCoreRunnerStartInput) -> StartRunRequest:
        """Convert one internal runner DTO into ``StartRunRequest``.

        Args:
            input_value: Platform request from the runner layer.

        Returns:
            A kernel-safe start request.

        """
        return StartRunRequest(
            initiator="agent_core_runner",
            run_kind=input_value.runner_kind,
            session_id=input_value.session_id,
            input_ref=input_value.goal_ref,
            input_json=input_value.goal_json,
            context_ref=input_value.context_ref,
        )

    def from_runner_child_spawn(
        self, input_value: AgentCoreChildSpawnInput
    ) -> SpawnChildRunRequest:
        """Convert one platform child DTO into ``SpawnChildRunRequest``.

        Args:
            input_value: Platform child spawn request.

        Returns:
            A kernel child run request.

        """
        return SpawnChildRunRequest(
            parent_run_id=input_value.parent_run_id,
            child_kind=input_value.runner_kind,
            input_ref=input_value.child_goal_ref,
            input_json=input_value.child_goal_json,
        )

    @staticmethod
    def _extract_session_id(session: Any | None) -> str | None:
        """Extract ``session_id`` from string/object inputs.

        Accepted forms:
          - plain string session id
          - object with callable ``session_id()``
          - object with string ``session_id`` attribute

        Returns:
            Normalized session id string if available; otherwise ``None``.

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

    @staticmethod
    def _extract_workflow_id(session: Any | None) -> str | None:
        """Extract ``workflow_id`` from object inputs when available.

        Accepted forms:
          - object with callable ``workflow_id()``
          - object with string ``workflow_id`` attribute

        Returns:
            Normalized workflow id string if available; otherwise ``None``.

        """
        if session is None:
            return None
        workflow_id_fn = getattr(session, "workflow_id", None)
        if callable(workflow_id_fn):
            value = workflow_id_fn()
            if value is not None:
                return str(value)
        workflow_id_attr = getattr(session, "workflow_id", None)
        if isinstance(workflow_id_attr, str):
            return workflow_id_attr
        return None
