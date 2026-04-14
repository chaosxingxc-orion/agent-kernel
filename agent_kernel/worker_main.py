"""Standalone Temporal worker entrypoint for agent-kernel.

Run directly::

    python -m agent_kernel.worker_main

Or via the installed CLI command::

    agent-kernel-worker

The worker hosts ``RunActorWorkflow`` on the configured Temporal task queue.
All connection and storage settings are read from environment variables with
the ``AGENT_KERNEL_`` prefix (see ``KernelConfig.from_env()``).

Key environment variables
--------------------------
``AGENT_KERNEL_TEMPORAL_HOST``
    Temporal frontend address (default: ``localhost:7233``).
``AGENT_KERNEL_TEMPORAL_NAMESPACE``
    Temporal namespace (default: ``default``).
``AGENT_KERNEL_TEMPORAL_TASK_QUEUE``
    Task queue the worker polls (default: ``agent-kernel``).
``AGENT_KERNEL_DATA_DIR``
    Directory for SQLite persistence files (default: ``/app/data``).
``AGENT_KERNEL_LLM_PROVIDER``
    LLM provider: ``"openai"`` or ``"anthropic"``.  Omit to disable cognitive.
``AGENT_KERNEL_LLM_MODEL``
    Model identifier passed to the provider API.
``AGENT_KERNEL_LLM_API_KEY``
    Provider API key.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.config import KernelConfig

logger = logging.getLogger(__name__)


def _build_llm_gateway(config: KernelConfig) -> Any | None:
    """Construct an LLM gateway from kernel config, or return None.

    Args:
        config: Populated ``KernelConfig`` instance.

    Returns:
        A concrete ``OpenAILLMGateway`` or ``AnthropicLLMGateway`` when
        ``config.llm_provider`` is ``"openai"`` or ``"anthropic"``
        respectively; ``None`` for any other value (including empty string).

    """
    if config.llm_provider not in ("openai", "anthropic"):
        return None
    from agent_kernel.kernel.cognitive.llm_gateway_config import (
        LLMGatewayConfig,
        create_llm_gateway,
    )

    gateway_config = LLMGatewayConfig(
        provider=config.llm_provider,  # type: ignore[arg-type]
        model=config.llm_model,
        api_key=config.llm_api_key,
    )
    return create_llm_gateway(gateway_config)


async def main() -> None:
    """Start the Temporal kernel worker and block until the SDK stops it.

    The Temporal Python SDK installs its own SIGTERM/SIGINT handlers inside
    ``worker.run()``, so no explicit signal wiring is required here.
    """
    from agent_kernel.config import KernelConfig
    from agent_kernel.runtime.bundle import (
        AgentKernelRuntimeBundle,
        RuntimeDedupeConfig,
        RuntimeEventLogConfig,
        RuntimeRecoveryOutcomeConfig,
        RuntimeStrictModeConfig,
        RuntimeTurnIntentLogConfig,
    )
    from agent_kernel.substrate.temporal.client import (
        TemporalClientConfig,
        create_temporal_client,
    )
    from agent_kernel.substrate.temporal.worker import TemporalWorkerConfig

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config = KernelConfig.from_env()
    data_dir = os.environ.get("AGENT_KERNEL_DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)

    logger.info(
        "Connecting to Temporal — host=%s namespace=%s",
        config.temporal_host,
        config.temporal_namespace,
    )
    client = await create_temporal_client(
        TemporalClientConfig(
            target_host=config.temporal_host,
            namespace=config.temporal_namespace,
        )
    )

    llm_gateway = _build_llm_gateway(config)
    if llm_gateway is not None:
        logger.info(
            "LLM gateway configured — provider=%s model=%s",
            config.llm_provider,
            config.llm_model,
        )
    else:
        logger.info(
            "No LLM gateway configured (llm_provider=%r); cognitive services disabled.",
            config.llm_provider,
        )

    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=client,
        event_log_config=RuntimeEventLogConfig(
            backend="sqlite",
            sqlite_database_path=f"{data_dir}/event_log.db",
        ),
        dedupe_config=RuntimeDedupeConfig(
            backend="sqlite",
            sqlite_database_path=f"{data_dir}/dedupe.db",
        ),
        recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
            backend="sqlite",
            sqlite_database_path=f"{data_dir}/recovery.db",
        ),
        turn_intent_log_config=RuntimeTurnIntentLogConfig(
            backend="sqlite",
            sqlite_database_path=f"{data_dir}/turn_intent.db",
        ),
        strict_mode_config=RuntimeStrictModeConfig(
            history_event_threshold=config.history_reset_threshold,
        ),
        llm_gateway=llm_gateway,
    )

    worker = bundle.create_temporal_worker(
        client,
        config=TemporalWorkerConfig(task_queue=config.temporal_task_queue),
    )

    logger.info(
        "Temporal worker started — task_queue=%s",
        config.temporal_task_queue,
    )
    # worker.run() blocks until the Temporal SDK receives a stop signal
    # (SIGTERM / SIGINT), drains in-flight tasks, then returns.
    await worker.run()
    logger.info("Temporal worker stopped.")


def main_sync() -> None:
    """Synchronous entry point for the ``agent-kernel-worker`` CLI command."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
