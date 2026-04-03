"""Failure evidence normalization and deterministic precedence selection.

This module is intentionally small and side-effect free. The recovery pipeline
can call it anywhere that needs a stable "best evidence" reference while
preserving the full raw envelope fields for auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import FailureEnvelope

FailureEvidenceSource = Literal[
    "external_ack_ref",
    "evidence_ref",
    "local_inference",
    "none",
]


@dataclass(frozen=True, slots=True)
class FailureEvidenceResolution:
    """Represents the selected evidence reference and its authoritative source.

    Attributes:
        source: Selected evidence source following v6.4 priority order.
        evidence_ref: Normalized evidence reference string, or ``None``.
    """

    source: FailureEvidenceSource
    evidence_ref: str | None


def resolve_failure_evidence(envelope: FailureEnvelope) -> FailureEvidenceResolution:
    """Resolves evidence using the v6.4 priority order.

    Priority is strict and deterministic:
    1. ``external_ack_ref``: strongest external confirmation.
    2. ``evidence_ref``: persisted or uploaded evidence artifact.
    3. ``local_inference``: local-only fallback hint.

    Args:
        envelope: Failure envelope containing raw evidence candidates.

    Returns:
        Selected evidence source and normalized evidence reference.
    """
    external_ack_ref = _normalize_optional_ref(envelope.external_ack_ref)
    if external_ack_ref is not None:
        return FailureEvidenceResolution(
            source="external_ack_ref",
            evidence_ref=external_ack_ref,
        )

    evidence_ref = _normalize_optional_ref(envelope.evidence_ref)
    if evidence_ref is not None:
        return FailureEvidenceResolution(
            source="evidence_ref",
            evidence_ref=evidence_ref,
        )

    local_inference = _normalize_optional_ref(envelope.local_inference)
    if local_inference is not None:
        return FailureEvidenceResolution(
            source="local_inference",
            evidence_ref=local_inference,
        )

    return FailureEvidenceResolution(source="none", evidence_ref=None)


def apply_failure_evidence_priority(envelope: FailureEnvelope) -> FailureEnvelope:
    """Returns envelope with resolved evidence-priority fields populated.

    This keeps the original candidate fields untouched and writes canonical
    resolution into explicit fields, so downstream consumers do not need to
    re-implement precedence logic.

    Args:
        envelope: Raw failure envelope.

    Returns:
        Copy of envelope with ``evidence_priority_source`` and
        ``evidence_priority_ref`` set from deterministic resolution.
    """
    resolution = resolve_failure_evidence(envelope)
    return replace(
        envelope,
        evidence_priority_source=resolution.source,
        evidence_priority_ref=resolution.evidence_ref,
    )


def _normalize_optional_ref(value: str | None) -> str | None:
    """Normalizes optional references by trimming whitespace and empties.

    Args:
        value: Optional reference string to normalize.

    Returns:
        Trimmed non-empty string, or ``None`` when input is ``None`` or empty.
    """
    if value is None:
        return None
    normalized_value = value.strip()
    if normalized_value == "":
        return None
    return normalized_value
