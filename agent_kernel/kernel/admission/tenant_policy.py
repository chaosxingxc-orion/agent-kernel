"""Tenant policy definitions and resolution for admission gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class PolicyResolutionError(ValueError):
    """Raised when a policy_ref cannot be resolved to a TenantPolicy."""


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    """Immutable tenant-level admission policy constraints.

    Attributes:
        policy_id: Stable identifier for this policy instance.
        max_allowed_risk_tier: Highest risk tier an action may declare (inclusive).
        allow_non_idempotent_remote_writes: Whether non-idempotent remote writes are permitted.
        max_actions_per_minute: Maximum admitted actions per run per 60-second sliding window.

    """

    policy_id: str
    max_allowed_risk_tier: int = 3
    allow_non_idempotent_remote_writes: bool = False
    max_actions_per_minute: int = 120


_DEFAULT_POLICY = TenantPolicy(policy_id="default")


class TenantPolicyResolver:
    """Resolves tenant_policy_ref strings to TenantPolicy instances.

    Supported ref formats:
    - ``"policy:default"`` — conservative built-in defaults.
    - ``"file:///absolute/path/to/policy.json"`` — read a JSON file whose keys
      match the TenantPolicy field names.
    - Any other string — raises PolicyResolutionError.

    """

    def resolve(self, policy_ref: str) -> TenantPolicy:
        """Resolve a policy reference string to a TenantPolicy.

        Args:
            policy_ref: Policy reference string in a supported format.

        Returns:
            Resolved TenantPolicy instance.

        Raises:
            PolicyResolutionError: When the ref format is unrecognised or the
                file cannot be read / parsed.

        """
        if policy_ref == "policy:default":
            return _DEFAULT_POLICY

        if policy_ref.startswith("file://"):
            return self._resolve_file(policy_ref)

        raise PolicyResolutionError(
            f"Unknown policy_ref format: {policy_ref!r}. "
            "Supported formats: 'policy:default', 'file:///absolute/path/to/policy.json'."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_file(self, policy_ref: str) -> TenantPolicy:
        """Read and parse a JSON policy file referenced by a file:// URI."""
        path_str = policy_ref[len("file://") :]
        path = Path(path_str)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyResolutionError(f"Cannot read policy file {path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolicyResolutionError(f"Policy file {path} is not valid JSON: {exc}") from exc

        try:
            return TenantPolicy(
                policy_id=data.get("policy_id", path.stem),
                max_allowed_risk_tier=int(data.get("max_allowed_risk_tier", 3)),
                allow_non_idempotent_remote_writes=bool(
                    data.get("allow_non_idempotent_remote_writes", False)
                ),
                max_actions_per_minute=int(data.get("max_actions_per_minute", 120)),
            )
        except (TypeError, ValueError) as exc:
            raise PolicyResolutionError(
                f"Policy file {path} contains invalid field values: {exc}"
            ) from exc
