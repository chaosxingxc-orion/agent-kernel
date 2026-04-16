"""Standard idempotency-key generation policy for dispatch and compensation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_kernel.kernel.contracts import Action


class IdempotencyKeyPolicy:
    """Generate deterministic idempotency keys from semantic action identity."""

    @staticmethod
    def generate(
        run_id: str,
        action: Action,
        snapshot_hash: str,
        namespace: str = "dispatch",
    ) -> str:
        """Generate canonical dispatch idempotency key.

        Format:
            ``{namespace}:{run_id}:{action_id}:{content_hash}``
        """
        content_hash = IdempotencyKeyPolicy._content_hash(action, snapshot_hash)
        return f"{namespace}:{run_id}:{action.action_id}:{content_hash}"

    @staticmethod
    def generate_compensation_key(effect_class: str, action_id: str) -> str:
        """Generate deterministic compensation idempotency key."""
        return f"compensation:{effect_class}:{action_id}"

    @staticmethod
    def _content_hash(action: Action, snapshot_hash: str) -> str:
        """Returns a stable content hash for idempotency inputs."""
        input_json_value = getattr(action, "input_json", None)
        if not isinstance(input_json_value, dict):
            input_json_value = {}

        policy_tags_value = getattr(action, "policy_tags", [])
        if isinstance(policy_tags_value, (list, tuple, set, frozenset)):
            policy_tags = [str(tag) for tag in policy_tags_value]
            if isinstance(policy_tags_value, (set, frozenset)):
                policy_tags.sort()
        else:
            policy_tags = []

        payload: dict[str, Any] = {
            "action_type": str(getattr(action, "action_type", "")),
            "input_json": IdempotencyKeyPolicy._canonicalize_json(input_json_value),
            "policy_tags": policy_tags,
            "effect_class": str(getattr(action, "effect_class", "")),
            "snapshot_hash": snapshot_hash,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonicalize_json(value: Any) -> Any:
        """Convert arbitrary nested values into deterministic JSON-safe values."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, dict):
            return {
                str(key): IdempotencyKeyPolicy._canonicalize_json(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [IdempotencyKeyPolicy._canonicalize_json(item) for item in value]
        if isinstance(value, (set, frozenset)):
            normalized = [IdempotencyKeyPolicy._canonicalize_json(item) for item in value]
            return sorted(normalized, key=lambda item: repr(item))
        return repr(value)
