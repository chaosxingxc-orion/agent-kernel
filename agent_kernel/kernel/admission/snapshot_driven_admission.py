"""Production snapshot-driven admission service for kernel action dispatch."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

from agent_kernel.kernel.admission.tenant_policy import TenantPolicy, TenantPolicyResolver
from agent_kernel.kernel.contracts import Action, AdmissionResult

if TYPE_CHECKING:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot


class SnapshotDrivenAdmissionService:
    """Evaluates action admission against a frozen CapabilitySnapshot.

    Implements the ``DispatchAdmissionService`` Protocol using a 5-rule
    pipeline executed in order; the first DENY wins.

    Rule execution order:
        1. Permission mode — deny non-read actions in readonly mode.
        2. Binding existence — deny actions targeting unbound tools/MCP/peers.
        3. Idempotency — deny non-idempotent remote writes when policy forbids.
        4. Risk tier — deny actions whose risk_tier exceeds policy maximum.
        5. Rate limit — deny when the run's per-minute action count is exceeded.

    Args:
        policy_resolver: Optional resolver; a default TenantPolicyResolver is
            used when None is supplied.

    """

    def __init__(self, policy_resolver: TenantPolicyResolver | None = None) -> None:
        self._resolver = policy_resolver if policy_resolver is not None else TenantPolicyResolver()
        # Sliding-window rate limiter: maps run_id → deque of admit timestamps (float).
        self._rate_windows: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # DispatchAdmissionService Protocol
    # ------------------------------------------------------------------

    async def admit(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
    ) -> AdmissionResult:
        """Evaluate whether an action may execute under the capability snapshot.

        Executes the 5-rule pipeline in order; the first deny rule wins.

        Args:
            action: Candidate action to evaluate.
            snapshot: Frozen capability snapshot used for policy checks.

        Returns:
            AdmissionResult with ``admitted=True`` and ``reason_code="ok"`` when
            all rules pass, or ``admitted=False`` with a specific reason code on
            the first rule violation.

        """
        policy_ref = getattr(snapshot, "tenant_policy_ref", "policy:default") or "policy:default"
        policy = self._resolver.resolve(policy_ref)

        # Rule 1 — Permission Mode
        result = self._check_permission_mode(action, snapshot)
        if result is not None:
            return result

        # Rule 2 — Binding Existence
        result = self._check_binding_existence(action, snapshot)
        if result is not None:
            return result

        # Rule 3 — Idempotency
        result = self._check_idempotency(action, policy)
        if result is not None:
            return result

        # Rule 4 — Risk Tier
        result = self._check_risk_tier(action, policy)
        if result is not None:
            return result

        # Rule 5 — Rate Limit
        result = self._check_rate_limit(action, policy)
        if result is not None:
            return result

        return AdmissionResult(admitted=True, reason_code="ok")

    # ------------------------------------------------------------------
    # Rule implementations
    # ------------------------------------------------------------------

    def _check_permission_mode(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
    ) -> AdmissionResult | None:
        """Deny non-read actions when the snapshot is in readonly mode."""
        permission_mode = getattr(snapshot, "permission_mode", None) or ""
        if permission_mode in ("readonly", "read_only"):
            action_type = getattr(action, "action_type", None)
            if action_type not in ("noop", "read"):
                return AdmissionResult(admitted=False, reason_code="permission_denied")
        return None

    def _check_binding_existence(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
    ) -> AdmissionResult | None:
        """Deny actions targeting tools, MCP servers, or peers that are not bound."""
        action_type = getattr(action, "action_type", None)

        if action_type == "tool_call":
            tool_bindings: list[str] = getattr(snapshot, "tool_bindings", None) or []
            tool_name = getattr(action, "tool_name", None)
            if tool_name is not None and tool_name not in tool_bindings:
                return AdmissionResult(admitted=False, reason_code="policy_denied")

        elif action_type == "mcp_call":
            mcp_bindings: list[str] = getattr(snapshot, "mcp_bindings", None) or []
            mcp_server = getattr(action, "mcp_server", None)
            mcp_capability = getattr(action, "mcp_capability", None)
            # MCP bindings are stored as "server/capability" composite strings.
            if mcp_server is not None and mcp_capability is not None:
                key = f"{mcp_server}/{mcp_capability}"
                if key not in mcp_bindings:
                    return AdmissionResult(admitted=False, reason_code="policy_denied")

        elif action_type == "peer_signal":
            peer_run_bindings: list[str] | None = getattr(snapshot, "peer_run_bindings", None)
            if peer_run_bindings:  # non-empty list means production-tier allowlist is active
                peer_run_id = getattr(action, "peer_run_id", None)
                if peer_run_id is not None and peer_run_id not in peer_run_bindings:
                    return AdmissionResult(admitted=False, reason_code="policy_denied")

        return None

    def _check_idempotency(
        self,
        action: Action,
        policy: TenantPolicy,
    ) -> AdmissionResult | None:
        """Deny non-idempotent remote writes when policy forbids them."""
        if not policy.allow_non_idempotent_remote_writes:
            idempotency_level = getattr(action, "idempotency_level", None)
            effect_class = getattr(action, "effect_class", None)
            # Support both string and StrEnum values.
            effect_class_val = str(effect_class) if effect_class is not None else None
            if idempotency_level == "non_idempotent" and effect_class_val in (
                "remote_write",
                "irreversible_write",
            ):
                return AdmissionResult(admitted=False, reason_code="policy_denied")
        return None

    def _check_risk_tier(
        self,
        action: Action,
        policy: TenantPolicy,
    ) -> AdmissionResult | None:
        """Deny actions whose declared risk_tier exceeds the policy maximum."""
        risk_tier = getattr(action, "risk_tier", 0) or 0
        if risk_tier > policy.max_allowed_risk_tier:
            return AdmissionResult(admitted=False, reason_code="policy_denied")
        return None

    def _check_rate_limit(
        self,
        action: Action,
        policy: TenantPolicy,
    ) -> AdmissionResult | None:
        """Deny actions when the run's sliding 60-second window is exhausted."""
        run_id: str = getattr(action, "run_id", "") or ""
        now = time.monotonic()
        window_start = now - 60.0

        if run_id not in self._rate_windows:
            self._rate_windows[run_id] = deque()

        window = self._rate_windows[run_id]
        # Evict timestamps older than 60 seconds.
        while window and window[0] < window_start:
            window.popleft()

        if len(window) >= policy.max_actions_per_minute:
            return AdmissionResult(admitted=False, reason_code="quota_exceeded")

        # Record this admission timestamp only when the action will be admitted.
        window.append(now)
        return None
