"""Capability snapshot input resolver for v6.4 source normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import Action
    from agent_kernel.kernel.turn_engine import TurnInput

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuildError,
    CapabilitySnapshotInput,
    DeclarativeBundleDigest,
)


@dataclass(frozen=True, slots=True)
class ActionPayloadCapabilitySnapshotInputResolver:
    """Resolves snapshot inputs from action payload with v6.4 priority rules.

    Priority model:
      pinned policy > approval > budget/quota > capability bindings
      > session mode > feature flags

    Resolver supports both:
      - structured payload under ``capability_snapshot_input``
      - flat compatibility fields under the same object
    """

    default_tenant_policy_ref: str = "policy:default"
    default_permission_mode: str = "strict"
    require_declared_snapshot_input: bool = False
    require_declarative_bundle_digest: bool = False

    def resolve(
        self,
        input_value: TurnInput,
        action: Action,
    ) -> CapabilitySnapshotInput:
        """Build CapabilitySnapshotInput from action payload.

        Args:
            input_value: Turn input providing run identity and offsets.
            action: Action carrying optional declared snapshot payload.

        Returns:
            Normalized snapshot input for capability snapshot construction.

        Raises:
            CapabilitySnapshotBuildError: When strict mode is enabled and
                required fields are missing from the action payload.

        """
        input_json = action.input_json if isinstance(action.input_json, dict) else {}
        raw_payload = input_json.get("capability_snapshot_input")
        if self.require_declared_snapshot_input and not isinstance(raw_payload, dict):
            raise CapabilitySnapshotBuildError(
                "capability_snapshot_input is required in strict mode."
            )
        payload = raw_payload if isinstance(raw_payload, dict) else {}

        policy = _as_dict(payload.get("policy"))
        approval = _as_dict(payload.get("approval"))
        budget = _as_dict(payload.get("budget"))
        bindings = _as_dict(payload.get("capability_bindings"))
        session = _as_dict(payload.get("session"))
        context = _as_dict(payload.get("context"))

        tenant_policy_ref = _first_non_empty_string(
            policy.get("tenant_policy_ref"),
            payload.get("tenant_policy_ref"),
            self.default_tenant_policy_ref,
        )
        permission_mode = _first_non_empty_string(
            policy.get("permission_mode"),
            payload.get("permission_mode"),
            self.default_permission_mode,
        )
        declarative_bundle_digest = _resolve_declarative_bundle_digest(payload)
        if self.require_declarative_bundle_digest and declarative_bundle_digest is None:
            raise CapabilitySnapshotBuildError(
                "declarative_bundle_digest is required in strict mode."
            )

        return CapabilitySnapshotInput(
            run_id=input_value.run_id,
            based_on_offset=input_value.based_on_offset,
            tenant_policy_ref=tenant_policy_ref,
            permission_mode=permission_mode,
            tool_bindings=_as_str_list(
                bindings.get("tool_bindings", payload.get("tool_bindings", []))
            ),
            mcp_bindings=_as_str_list(
                bindings.get("mcp_bindings", payload.get("mcp_bindings", []))
            ),
            skill_bindings=_as_str_list(
                bindings.get("skill_bindings", payload.get("skill_bindings", []))
            ),
            feature_flags=_as_str_list(payload.get("feature_flags", [])),
            context_binding_ref=_first_non_empty_optional_string(
                context.get("context_binding_ref"),
                payload.get("context_binding_ref"),
            ),
            context_content_hash=_first_non_empty_optional_string(
                context.get("context_content_hash"),
                payload.get("context_content_hash"),
            ),
            budget_ref=_first_non_empty_optional_string(
                budget.get("budget_ref"),
                payload.get("budget_ref"),
            ),
            quota_ref=_first_non_empty_optional_string(
                budget.get("quota_ref"),
                payload.get("quota_ref"),
            ),
            session_mode=_first_non_empty_optional_string(
                session.get("session_mode"),
                payload.get("session_mode"),
            ),
            approval_state=_first_non_empty_optional_string(
                approval.get("approval_state"),
                payload.get("approval_state"),
            ),
            declarative_bundle_digest=declarative_bundle_digest,
        )


def _as_dict(value: Any) -> dict[str, Any]:
    """Return value as dict or empty dict.

    Args:
        value: Candidate value to coerce.

    Returns:
        ``value`` when it is a dict, otherwise an empty dict.

    """
    if isinstance(value, dict):
        return value
    return {}


def _as_str_list(value: Any) -> list[str]:
    """Return value as list of strings, dropping empty and non-string items.

    Args:
        value: Candidate value to coerce.

    Returns:
        List of non-empty strings, or empty list when value is not a list.

    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _first_non_empty_string(*values: Any) -> str:
    """Return first non-empty string candidate or empty fallback.

    Args:
        *values: Ordered candidate values to inspect.

    Returns:
        First non-empty string, or empty string when all candidates are empty.

    """
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _first_non_empty_optional_string(*values: Any) -> str | None:
    """Return first non-empty string candidate or ``None``.

    Args:
        *values: Ordered candidate values to inspect.

    Returns:
        First non-empty string, or ``None`` when all candidates are empty.

    """
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_declarative_bundle_digest(
    payload: dict[str, Any],
) -> DeclarativeBundleDigest | None:
    """Resolve one declarative bundle digest payload when present.

    Args:
        payload: Action payload dictionary containing digest fields.

    Returns:
        Typed digest when all required fields are present, or ``None``
        when no digest fields exist in the payload.

    Raises:
        CapabilitySnapshotBuildError: When some but not all required
            digest fields are present.

    """
    raw_digest_payload = payload.get("declarative_bundle_digest")
    if raw_digest_payload is not None and not isinstance(raw_digest_payload, dict):
        raise CapabilitySnapshotBuildError(
            "declarative_bundle_digest must be an object when provided."
        )
    digest_payload = _as_dict(raw_digest_payload)

    bundle_ref = _first_non_empty_optional_string(
        digest_payload.get("bundle_ref"),
        payload.get("declarative_bundle_ref"),
    )
    semantics_version = _first_non_empty_optional_string(
        digest_payload.get("semantics_version"),
        payload.get("declarative_bundle_semantics_version"),
    )
    content_hash = _first_non_empty_optional_string(
        digest_payload.get("content_hash"),
        payload.get("declarative_bundle_content_hash"),
    )
    compile_hash = _first_non_empty_optional_string(
        digest_payload.get("compile_hash"),
        payload.get("declarative_bundle_compile_hash"),
    )

    digest_fields = {
        "bundle_ref": bundle_ref,
        "semantics_version": semantics_version,
        "content_hash": content_hash,
        "compile_hash": compile_hash,
    }
    if all(field_value is None for field_value in digest_fields.values()):
        return None

    missing_fields = [
        field_name for field_name, field_value in digest_fields.items() if field_value is None
    ]
    if missing_fields:
        missing_field_names = ", ".join(missing_fields)
        raise CapabilitySnapshotBuildError(
            f"declarative_bundle_digest is missing required fields: {missing_field_names}."
        )

    return DeclarativeBundleDigest(
        bundle_ref=bundle_ref or "",
        semantics_version=semantics_version or "",
        content_hash=content_hash or "",
        compile_hash=compile_hash or "",
    )
