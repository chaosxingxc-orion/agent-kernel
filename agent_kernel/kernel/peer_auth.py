"""Peer-run signal authorization for agent-kernel.

Determines whether a ``from_peer_run_id`` is authorized to send signals into
the current run.  Two authorization tiers are supported:

Production tier (``peer_run_bindings`` non-empty in snapshot):
    Explicit allowlist of pre-approved peer run identifiers stored in the
    CapabilitySnapshot.  Created by the platform when two runs are paired
    (A2A handshake, parent-child delegation, etc.).

PoC / fallback tier (``peer_run_bindings`` empty):
    Falls back to checking ``active_child_runs`` from the current run
    projection.  This is adequate for PoC and single-worker scenarios but is
    not cryptographically sound for multi-tenant deployments because
    ``active_child_runs`` is mutable projection state, not an immutable
    policy declaration.

Usage::

    from agent_kernel.kernel.peer_auth import is_peer_run_authorized

    authorized = is_peer_run_authorized(
        peer_run_id="child-run-abc",
        snapshot=snapshot,
        active_child_runs=projection.active_child_runs,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot

__all__ = ["is_peer_run_authorized"]


def is_peer_run_authorized(
    peer_run_id: str,
    snapshot: CapabilitySnapshot,
    active_child_runs: list[str] | None = None,
) -> bool:
    """Return True when *peer_run_id* is authorized to signal this run.

    Authorization priority:
    1. **Production tier**: If ``snapshot.peer_run_bindings`` is non-empty,
       the peer must be in the bindings list.  The bindings are established at
       run-creation time and are part of the immutable snapshot hash.
    2. **PoC fallback**: If ``peer_run_bindings`` is empty, check
       ``active_child_runs`` from the live projection.  Suitable for PoC /
       single-worker deployments only.

    Args:
        peer_run_id: Run id of the peer sending the signal.
        snapshot: Frozen capability snapshot for the receiving run.  Must have
            ``peer_run_bindings`` field (added in schema_version ``"2"``).
        active_child_runs: Current list of active child run ids from the
            run projection.  Used as PoC fallback when
            ``snapshot.peer_run_bindings`` is empty.

    Returns:
        ``True`` when *peer_run_id* is authorized; ``False`` otherwise.
    """
    bindings: list[str] = getattr(snapshot, "peer_run_bindings", [])
    if bindings:
        # Production tier: explicit, policy-declared allowlist.
        return peer_run_id in bindings

    # PoC fallback: projection-derived active children.
    if active_child_runs:
        return peer_run_id in active_child_runs

    return False
