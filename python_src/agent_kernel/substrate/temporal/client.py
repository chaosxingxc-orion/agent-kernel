"""Temporal client factory for kernel runtime substrate wiring.

This module isolates Temporal SDK import and connection concerns so that:
  - business modules do not depend on SDK internals,
  - tests can mock gateway/client boundaries cleanly,
  - deployment wiring remains explicit and configurable.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True, slots=True)
class TemporalClientConfig:
    """Connection settings for Temporal client creation.

    Attributes:
        target_host: Temporal frontend endpoint, such as ``localhost:7233``.
        namespace: Temporal namespace for kernel workflows.
    """

    target_host: str = "localhost:7233"
    namespace: str = "default"


async def create_temporal_client(
    config: TemporalClientConfig | None = None,
) -> Any:
    """Creates and returns a connected Temporal SDK client.

    Args:
        config: Optional client connection configuration. Uses defaults
            when not provided.

    Returns:
        Connected Temporal SDK client instance.

    Raises:
        RuntimeError: If Temporal Python SDK is not installed.
    """
    active_config = config or TemporalClientConfig()
    try:
        client_module = import_module("temporalio.client")
        client_cls = client_module.Client
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Temporal SDK is required."
            " Install dependency: temporalio"
        ) from exc
    return await client_cls.connect(
        active_config.target_host,
        namespace=active_config.namespace,
    )
