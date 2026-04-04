"""CapabilitySnapshot canonicalization and stable hash implementation for v6.4.

This module intentionally keeps the builder deterministic and narrow:
  - It accepts only declared inputs.
  - It canonicalizes list-like fields using stable sort + dedupe.
  - It hashes a canonical JSON object with SHA256 for cross-process stability.

The builder does not act as policy authority. It only normalizes and freezes
capability inputs into a replay-safe execution snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


class CapabilitySnapshotBuildError(ValueError):
    """Raised when mandatory snapshot inputs are missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class DeclarativeBundleDigest:
    """Represents a compiled declarative rule bundle signature.

    Attributes:
        bundle_ref: Stable logical bundle reference.
        semantics_version: Semantic contract version of the bundle.
        content_hash: Canonical content hash before compilation.
        compile_hash: Compiler-output hash to pin compiled semantics.
    """

    bundle_ref: str
    semantics_version: str
    content_hash: str
    compile_hash: str


@dataclass(frozen=True, slots=True)
class CapabilitySnapshotInput:
    """Represents declared capability inputs that participate in snapshot hash.

    Attributes:
        run_id: Kernel run identifier.
        based_on_offset: Baseline projection offset for the snapshot.
        tenant_policy_ref: Reference to the governing tenant policy.
        permission_mode: Permission evaluation mode.
        tool_bindings: Ordered list of bound tool names.
        mcp_bindings: Ordered list of bound MCP server capabilities.
        skill_bindings: Ordered list of bound skill references.
        feature_flags: Active feature flags for the snapshot.
        context_binding_ref: Optional context binding reference.
        context_content_hash: Optional stable hash of context content.
        budget_ref: Optional budget allocation reference.
        quota_ref: Optional quota allocation reference.
        session_mode: Optional session mode discriminator.
        approval_state: Optional approval gate state.
        declarative_bundle_digest: Optional compiled rule bundle signature.
    """

    run_id: str
    based_on_offset: int
    tenant_policy_ref: str
    permission_mode: str
    tool_bindings: list[str] = field(default_factory=list)
    mcp_bindings: list[str] = field(default_factory=list)
    skill_bindings: list[str] = field(default_factory=list)
    feature_flags: list[str] = field(default_factory=list)
    context_binding_ref: str | None = None
    context_content_hash: str | None = None
    budget_ref: str | None = None
    quota_ref: str | None = None
    session_mode: str | None = None
    approval_state: str | None = None
    declarative_bundle_digest: DeclarativeBundleDigest | None = None


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Represents a frozen capability view for one run + projection offset.

    Attributes:
        snapshot_ref: Stable reference identifier for the snapshot.
        snapshot_hash: Deterministic SHA256 hash of canonical payload.
        run_id: Kernel run identifier.
        based_on_offset: Baseline projection offset.
        tenant_policy_ref: Governing tenant policy reference.
        permission_mode: Permission evaluation mode.
        tool_bindings: Normalized tool binding list.
        mcp_bindings: Normalized MCP binding list.
        skill_bindings: Normalized skill binding list.
        feature_flags: Normalized feature flag list.
        context_binding_ref: Optional context binding reference.
        context_content_hash: Optional context content hash.
        budget_ref: Optional budget reference.
        quota_ref: Optional quota reference.
        session_mode: Optional session mode.
        approval_state: Optional approval state.
        declarative_bundle_digest: Optional rule bundle signature.
        created_at: RFC3339 UTC creation timestamp.
    """

    snapshot_ref: str
    snapshot_hash: str
    run_id: str
    based_on_offset: int
    tenant_policy_ref: str
    permission_mode: str
    tool_bindings: list[str]
    mcp_bindings: list[str]
    skill_bindings: list[str]
    feature_flags: list[str]
    context_binding_ref: str | None = None
    context_content_hash: str | None = None
    budget_ref: str | None = None
    quota_ref: str | None = None
    session_mode: str | None = None
    approval_state: str | None = None
    declarative_bundle_digest: DeclarativeBundleDigest | None = None
    created_at: str = ""
    snapshot_schema_version: str = "1"


_CURRENT_SNAPSHOT_SCHEMA_VERSION = "1"
"""Current canonical schema version — see ``assert_snapshot_compatible`` for details."""


class CapabilitySnapshotBuilder:
    """Builds canonical CapabilitySnapshot objects using deterministic hashing.

    Design guarantees:
      1. Same semantic inputs produce same ``snapshot_hash``.
      2. Unordered input lists are normalized before hashing.
      3. Mutable references do not override stable content hashes for context.
    """

    def build(self, input_value: CapabilitySnapshotInput) -> CapabilitySnapshot:
        """Builds one canonical snapshot from declared input fields.

        Args:
            input_value: Snapshot input from capability assembly pipeline.

        Returns:
            Canonical, hash-stamped ``CapabilitySnapshot``.

        Raises:
            CapabilitySnapshotBuildError: If required fields are missing.

        Note:
            The caller must not mutate ``input_value`` after passing it here.
            If the same ``CapabilitySnapshotInput`` instance is reused across
            multiple ``build()`` calls with intervening mutations to its list
            fields, the resulting hashes will differ even though the object
            reference appears identical — use ``dataclasses.replace()`` to
            produce a new instance before mutating.
        """
        self._validate_input(input_value)

        normalized_tool_bindings = _normalize_string_list(input_value.tool_bindings)
        normalized_mcp_bindings = _normalize_string_list(input_value.mcp_bindings)
        normalized_skill_bindings = _normalize_string_list(input_value.skill_bindings)
        normalized_feature_flags = _normalize_string_list(input_value.feature_flags)

        canonical_payload = {
            "snapshot_schema_version": _CURRENT_SNAPSHOT_SCHEMA_VERSION,
            "run_id": input_value.run_id,
            "based_on_offset": input_value.based_on_offset,
            "tenant_policy_ref": input_value.tenant_policy_ref,
            "permission_mode": input_value.permission_mode,
            "tool_bindings": normalized_tool_bindings,
            "mcp_bindings": normalized_mcp_bindings,
            "skill_bindings": normalized_skill_bindings,
            "feature_flags": normalized_feature_flags,
            "context_content_hash": input_value.context_content_hash,
            "budget_ref": input_value.budget_ref,
            "quota_ref": input_value.quota_ref,
            "session_mode": input_value.session_mode,
            "approval_state": input_value.approval_state,
            "declarative_bundle_digest": _normalize_bundle_digest(
                input_value.declarative_bundle_digest
            ),
        }
        snapshot_hash = _build_stable_sha256(canonical_payload)

        snapshot_ref = (
            f"snapshot:{input_value.run_id}:{input_value.based_on_offset}:{snapshot_hash[:12]}"
        )
        return CapabilitySnapshot(
            snapshot_ref=snapshot_ref,
            snapshot_hash=snapshot_hash,
            run_id=input_value.run_id,
            based_on_offset=input_value.based_on_offset,
            tenant_policy_ref=input_value.tenant_policy_ref,
            permission_mode=input_value.permission_mode,
            tool_bindings=normalized_tool_bindings,
            mcp_bindings=normalized_mcp_bindings,
            skill_bindings=normalized_skill_bindings,
            feature_flags=normalized_feature_flags,
            context_binding_ref=input_value.context_binding_ref,
            context_content_hash=input_value.context_content_hash,
            budget_ref=input_value.budget_ref,
            quota_ref=input_value.quota_ref,
            session_mode=input_value.session_mode,
            approval_state=input_value.approval_state,
            declarative_bundle_digest=input_value.declarative_bundle_digest,
            created_at=_utc_now_iso(),
            snapshot_schema_version=_CURRENT_SNAPSHOT_SCHEMA_VERSION,
        )

    def _validate_input(self, input_value: CapabilitySnapshotInput) -> None:
        """Validates mandatory fields for deterministic and safe snapshot builds.

        Args:
            input_value: Snapshot input to validate.

        Raises:
            CapabilitySnapshotBuildError: If required fields are missing
                or inconsistent.
        """
        if not input_value.run_id:
            raise CapabilitySnapshotBuildError("run_id is required.")
        if input_value.based_on_offset < 0:
            raise CapabilitySnapshotBuildError("based_on_offset must be >= 0.")
        if not input_value.tenant_policy_ref:
            raise CapabilitySnapshotBuildError("tenant_policy_ref is required.")
        if not input_value.permission_mode:
            raise CapabilitySnapshotBuildError("permission_mode is required.")
        if input_value.context_binding_ref and not input_value.context_content_hash:
            raise CapabilitySnapshotBuildError(
                "context_content_hash is required when context_binding_ref is set."
            )


def _normalize_bundle_digest(
    digest: DeclarativeBundleDigest | None,
) -> dict[str, str] | None:
    """Normalizes bundle digest into stable dict form for canonical hashing.

    Args:
        digest: Optional declarative bundle digest to normalize.

    Returns:
        Dictionary with bundle fields, or ``None`` when input is ``None``.
    """
    if digest is None:
        return None
    return {
        "bundle_ref": digest.bundle_ref,
        "semantics_version": digest.semantics_version,
        "content_hash": digest.content_hash,
        "compile_hash": digest.compile_hash,
    }


def _normalize_string_list(values: list[str]) -> list[str]:
    """Returns deterministic deduped and sorted string list.

    Args:
        values: Raw string list that may contain duplicates or empty strings.

    Returns:
        Sorted list with duplicates and empty strings removed.
    """
    unique_values = {value for value in values if value}
    return sorted(unique_values)


def _build_stable_sha256(payload: dict[str, object]) -> str:
    """Builds SHA256 from canonical JSON representation.

    Canonicalization strategy:
      - ``sort_keys=True`` to stabilize object field ordering.
      - ``separators=(',', ':')`` to remove formatting-dependent whitespace.
      - UTF-8 bytes as canonical digest input.

    Args:
        payload: Canonical dictionary to hash.

    Returns:
        Hex-encoded SHA256 digest string.
    """
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    """Returns RFC3339 UTC timestamp for snapshot creation time.

    Returns:
        UTC timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Schema version management
# ---------------------------------------------------------------------------


def assert_snapshot_compatible(snapshot: CapabilitySnapshot) -> None:
    """Raises ``ValueError`` if the snapshot was built with an unknown schema version.

    Call this at any point where a persisted or received snapshot is loaded
    back into the running kernel, to catch version mismatches early rather
    than silently misinterpreting stale hashes.

    Args:
        snapshot: Snapshot to validate.

    Raises:
        ValueError: When ``snapshot.snapshot_schema_version`` differs from
            the current kernel schema version.
    """
    if snapshot.snapshot_schema_version != _CURRENT_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"CapabilitySnapshot schema_version={snapshot.snapshot_schema_version!r} "
            f"is incompatible with the current kernel version "
            f"({_CURRENT_SNAPSHOT_SCHEMA_VERSION!r}). "
            "Rebuild the snapshot using CapabilitySnapshotBuilder."
        )
