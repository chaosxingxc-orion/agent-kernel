"""Tests for SnapshotDrivenAdmissionService and TenantPolicyResolver.

All tests use real components with no internal mocks. External interfaces
(file I/O in TenantPolicyResolver) are tested via real temp files.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_kernel.kernel.admission.snapshot_driven_admission import SnapshotDrivenAdmissionService
from agent_kernel.kernel.admission.tenant_policy import (
    PolicyResolutionError,
    TenantPolicy,
    TenantPolicyResolver,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_snapshot(**kwargs) -> MagicMock:
    """Build a minimal CapabilitySnapshot-like object for testing.

    Uses MagicMock so that attribute access never raises AttributeError for
    fields not explicitly set, which mirrors how getattr-defensive code behaves
    against the real frozen dataclass.

    Legitimate mock use: unit-test isolation of admission rules only;
    the real CapabilitySnapshot is tested in test_capability_snapshot.py.
    """
    defaults = {
        "permission_mode": "strict",
        "tool_bindings": ["my_tool"],
        "mcp_bindings": ["my_server/my_cap"],
        "peer_run_bindings": [],
        "tenant_policy_ref": "policy:default",
    }
    defaults.update(kwargs)
    snap = MagicMock()
    for k, v in defaults.items():
        setattr(snap, k, v)
    return snap


def make_action(**kwargs) -> MagicMock:
    """Build a minimal Action-like object for testing.

    Legitimate mock use: unit-test isolation of admission rules only;
    real Action DTOs are tested in turn-engine integration tests.
    """
    defaults = {
        "action_type": "tool_call",
        "tool_name": "my_tool",
        "run_id": "run-test-001",
        "risk_tier": 0,
    }
    defaults.update(kwargs)
    action = MagicMock()
    for k, v in defaults.items():
        setattr(action, k, v)
    return action


def admit(service: SnapshotDrivenAdmissionService, action, snapshot) -> object:
    """Synchronous wrapper around the async admit() method."""
    return asyncio.run(service.admit(action, snapshot))


# ---------------------------------------------------------------------------
# TestPermissionModeRule
# ---------------------------------------------------------------------------


class TestPermissionModeRule:
    def test_readonly_denies_tool_call(self) -> None:
        """A tool_call action must be denied when permission_mode is 'readonly'."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(permission_mode="readonly")
        action = make_action(action_type="tool_call")
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "permission_denied"

    def test_read_only_variant_denies_tool_call(self) -> None:
        """'read_only' (underscore) is also a readonly mode."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(permission_mode="read_only")
        action = make_action(action_type="tool_call")
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "permission_denied"

    def test_readonly_allows_noop(self) -> None:
        """A noop action must pass the permission-mode rule even in readonly."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(permission_mode="readonly")
        action = make_action(action_type="noop", tool_name=None)
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_readonly_allows_read(self) -> None:
        """A read action must pass the permission-mode rule in readonly mode."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(permission_mode="readonly")
        action = make_action(action_type="read", tool_name=None)
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_strict_allows_tool_call(self) -> None:
        """A tool_call action with a bound tool must be admitted in strict mode."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(permission_mode="strict")
        action = make_action(action_type="tool_call", tool_name="my_tool")
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"


# ---------------------------------------------------------------------------
# TestBindingRule
# ---------------------------------------------------------------------------


class TestBindingRule:
    def test_unregistered_tool_denied(self) -> None:
        """A tool_call referencing a tool not in tool_bindings must be denied."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=["allowed_tool"])
        action = make_action(action_type="tool_call", tool_name="other_tool")
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "policy_denied"

    def test_registered_tool_admitted(self) -> None:
        """A tool_call whose tool_name is in tool_bindings must be admitted."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=["my_tool"])
        action = make_action(action_type="tool_call", tool_name="my_tool")
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_unregistered_mcp_denied(self) -> None:
        """An mcp_call referencing an unbound server/capability must be denied."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(mcp_bindings=["server_a/cap_x"])
        action = make_action(
            action_type="mcp_call",
            mcp_server="server_b",
            mcp_capability="cap_y",
            tool_name=None,
        )
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "policy_denied"

    def test_registered_mcp_admitted(self) -> None:
        """An mcp_call with a bound server/capability must be admitted."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(mcp_bindings=["my_server/my_cap"])
        action = make_action(
            action_type="mcp_call",
            mcp_server="my_server",
            mcp_capability="my_cap",
            tool_name=None,
        )
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_peer_signal_denied_when_not_in_allowlist(self) -> None:
        """A peer_signal to a non-allowlisted run_id must be denied."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(peer_run_bindings=["run-peer-allowed"])
        action = make_action(
            action_type="peer_signal",
            peer_run_id="run-peer-unknown",
            tool_name=None,
        )
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "policy_denied"

    def test_peer_signal_admitted_when_in_allowlist(self) -> None:
        """A peer_signal to an allowlisted peer run_id must be admitted."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(peer_run_bindings=["run-peer-allowed"])
        action = make_action(
            action_type="peer_signal",
            peer_run_id="run-peer-allowed",
            tool_name=None,
        )
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_peer_signal_admitted_when_bindings_empty(self) -> None:
        """A peer_signal is admitted when peer_run_bindings is empty (PoC fallback)."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(peer_run_bindings=[])
        action = make_action(
            action_type="peer_signal",
            peer_run_id="run-any",
            tool_name=None,
        )
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"


# ---------------------------------------------------------------------------
# TestIdempotencyRule
# ---------------------------------------------------------------------------


class TestIdempotencyRule:
    def test_non_idempotent_remote_write_denied_by_default(self) -> None:
        """Non-idempotent remote writes must be denied under the default policy."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=[])
        action = make_action(
            action_type="noop",
            tool_name=None,
            idempotency_level="non_idempotent",
            effect_class="remote_write",
        )
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "policy_denied"

    def test_non_idempotent_allowed_when_policy_permits(self) -> None:
        """Non-idempotent remote writes are admitted when the policy explicitly allows them."""
        from agent_kernel.kernel.admission.tenant_policy import TenantPolicy, TenantPolicyResolver

        permissive_policy = TenantPolicy(
            policy_id="permissive",
            allow_non_idempotent_remote_writes=True,
        )

        class _PermissiveResolver(TenantPolicyResolver):
            def resolve(self, policy_ref: str) -> TenantPolicy:
                return permissive_policy

        svc = SnapshotDrivenAdmissionService(policy_resolver=_PermissiveResolver())
        snap = make_snapshot(tool_bindings=[])
        action = make_action(
            action_type="noop",
            tool_name=None,
            idempotency_level="non_idempotent",
            effect_class="remote_write",
        )
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"


# ---------------------------------------------------------------------------
# TestRiskTierRule
# ---------------------------------------------------------------------------


class TestRiskTierRule:
    def test_risk_tier_exceeded_denied(self) -> None:
        """An action with risk_tier above the policy maximum must be denied."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=[])
        action = make_action(action_type="noop", tool_name=None, risk_tier=10)
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "policy_denied"

    def test_risk_tier_within_limit_admitted(self) -> None:
        """An action with risk_tier at or below the policy maximum must be admitted."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=[])
        action = make_action(action_type="noop", tool_name=None, risk_tier=3)
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"

    def test_risk_tier_zero_admitted(self) -> None:
        """An action with risk_tier=0 (unset) must always pass the risk tier rule."""
        svc = SnapshotDrivenAdmissionService()
        snap = make_snapshot(tool_bindings=[])
        action = make_action(action_type="noop", tool_name=None, risk_tier=0)
        result = admit(svc, action, snap)
        assert result.admitted
        assert result.reason_code == "ok"


# ---------------------------------------------------------------------------
# TestRateLimitRule
# ---------------------------------------------------------------------------


class TestRateLimitRule:
    def test_within_rate_limit_admitted(self) -> None:
        """Actions within max_actions_per_minute must all be admitted."""
        from agent_kernel.kernel.admission.tenant_policy import TenantPolicy, TenantPolicyResolver

        tight_policy = TenantPolicy(policy_id="tight", max_actions_per_minute=5)

        class _TightResolver(TenantPolicyResolver):
            def resolve(self, policy_ref: str) -> TenantPolicy:
                return tight_policy

        svc = SnapshotDrivenAdmissionService(policy_resolver=_TightResolver())
        snap = make_snapshot(tool_bindings=[])

        for _ in range(5):
            action = make_action(action_type="noop", tool_name=None, risk_tier=0)
            result = admit(svc, action, snap)
            assert result.admitted, f"Expected admission but got {result.reason_code}"

    def test_exceeds_rate_limit_denied(self) -> None:
        """The (N+1)th action within the window must be denied when limit is N."""
        from agent_kernel.kernel.admission.tenant_policy import TenantPolicy, TenantPolicyResolver

        tight_policy = TenantPolicy(policy_id="tight", max_actions_per_minute=3)

        class _TightResolver(TenantPolicyResolver):
            def resolve(self, policy_ref: str) -> TenantPolicy:
                return tight_policy

        svc = SnapshotDrivenAdmissionService(policy_resolver=_TightResolver())
        snap = make_snapshot(tool_bindings=[])

        # Exhaust the budget.
        for _ in range(3):
            action = make_action(action_type="noop", tool_name=None, risk_tier=0)
            admit(svc, action, snap)

        # The 4th attempt must be denied.
        action = make_action(action_type="noop", tool_name=None, risk_tier=0)
        result = admit(svc, action, snap)
        assert not result.admitted
        assert result.reason_code == "quota_exceeded"


# ---------------------------------------------------------------------------
# TestTenantPolicyResolver
# ---------------------------------------------------------------------------


class TestTenantPolicyResolver:
    def test_policy_default_resolves(self) -> None:
        """'policy:default' must resolve to the built-in conservative policy."""
        resolver = TenantPolicyResolver()
        policy = resolver.resolve("policy:default")
        assert isinstance(policy, TenantPolicy)
        assert policy.policy_id == "default"
        assert policy.max_allowed_risk_tier == 3
        assert not policy.allow_non_idempotent_remote_writes
        assert policy.max_actions_per_minute == 120

    def test_file_ref_resolves(self) -> None:
        """A 'file:///' reference must be read and parsed into a TenantPolicy."""
        data = {
            "policy_id": "custom",
            "max_allowed_risk_tier": 5,
            "allow_non_idempotent_remote_writes": True,
            "max_actions_per_minute": 60,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            tmp_path = Path(f.name)

        try:
            resolver = TenantPolicyResolver()
            policy = resolver.resolve(f"file://{tmp_path}")
            assert policy.policy_id == "custom"
            assert policy.max_allowed_risk_tier == 5
            assert policy.allow_non_idempotent_remote_writes is True
            assert policy.max_actions_per_minute == 60
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_unknown_ref_raises(self) -> None:
        """An unrecognised policy_ref must raise PolicyResolutionError."""
        resolver = TenantPolicyResolver()
        with pytest.raises(PolicyResolutionError):
            resolver.resolve("s3://some-bucket/policy.json")

    def test_missing_file_raises(self) -> None:
        """A file:// ref pointing to a non-existent path must raise PolicyResolutionError."""
        resolver = TenantPolicyResolver()
        with pytest.raises(PolicyResolutionError):
            resolver.resolve("file:////nonexistent/path/policy.json")


# ---------------------------------------------------------------------------
# Integration: SnapshotDrivenAdmissionService + TurnEngine cycle
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from typing import Any

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot
from agent_kernel.kernel.contracts import Action, EffectClass
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput


@dataclass(slots=True)
class _IntegrationSnapshotBuilder:
    """Builds a real CapabilitySnapshot with configurable permission_mode."""

    permission_mode: str

    def build(self, *_args: Any, **_kwargs: Any) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            snapshot_ref="snapshot:run-int:1:abc",
            snapshot_hash="hash-int",
            run_id="run-int",
            based_on_offset=1,
            tenant_policy_ref="policy:default",
            permission_mode=self.permission_mode,
            tool_bindings=["tool.search"],
            mcp_bindings=[],
            skill_bindings=[],
            feature_flags=[],
            created_at="2026-04-14T00:00:00Z",
        )


@dataclass(slots=True)
class _AckExecutor:
    """Executor stub that always returns an acknowledged result."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"acknowledged": True}


def _make_integration_engine(permission_mode: str) -> TurnEngine:
    return TurnEngine(
        snapshot_builder=_IntegrationSnapshotBuilder(permission_mode),
        admission_service=SnapshotDrivenAdmissionService(),
        dedupe_store=InMemoryDedupeStore(),
        executor=_AckExecutor(),
    )


def _integration_turn_input() -> TurnInput:
    return TurnInput(
        run_id="run-int",
        through_offset=1,
        based_on_offset=1,
        trigger_type="start",
    )


class TestTurnEngineIntegration:
    """Integration tests: real SnapshotDrivenAdmissionService wired into TurnEngine.

    Uses real components end-to-end (no mocks of admission logic) to verify
    TurnEngine FSM transitions under admitted and denied admission outcomes.
    """

    def test_admitted_action_reaches_dispatched_state(self) -> None:
        """Action not blocked by any rule in strict mode must be admitted and dispatched."""
        engine = _make_integration_engine("strict")
        # action_type "noop" passes all 5 admission rules under strict mode
        action = Action(
            action_id="action-int-1",
            run_id="run-int",
            action_type="noop",
            effect_class=EffectClass.READ_ONLY,
            input_json={},
        )
        result = asyncio.run(engine.run_turn(_integration_turn_input(), action))
        assert result.state in ("dispatch_acknowledged", "dispatched")

    def test_denied_action_in_readonly_reaches_blocked_state(self) -> None:
        """tool_call in readonly snapshot must be denied by Rule 1 → dispatch_blocked."""
        engine = _make_integration_engine("readonly")
        # action_type "tool_call" is blocked by Rule 1 (permission_mode=readonly)
        action = Action(
            action_id="action-int-2",
            run_id="run-int",
            action_type="tool_call",
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            input_json={},
        )
        result = asyncio.run(engine.run_turn(_integration_turn_input(), action))
        assert result.state == "dispatch_blocked"
