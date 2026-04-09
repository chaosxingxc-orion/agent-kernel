"""Concurrency benchmark tests for the agent-kernel.

Validates high-concurrency capabilities of the kernel by exercising
concurrent run starts, signal delivery, projection queries, and event
consistency through KernelFacade backed by LocalWorkflowGateway.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    QueryRunRequest,
    SignalRunRequest,
    StartRunRequest,
)
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.substrate.local.adaptor import LocalWorkflowGateway
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
    configure_run_actor_dependencies,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kernel() -> KernelFacade:
    """Build a full KernelFacade with LocalWorkflowGateway and in-memory deps."""
    event_log = InMemoryKernelRuntimeEventLog()
    deps = RunActorDependencyBundle(
        event_log=event_log,
        projection=InMemoryDecisionProjectionService(event_log),
        admission=StaticDispatchAdmissionService(),
        executor=AsyncExecutorService(),
        recovery=StaticRecoveryGateService(),
        deduper=InMemoryDecisionDeduper(),
        dedupe_store=InMemoryDedupeStore(),
        strict_mode=RunActorStrictModeConfig(enabled=False),
        workflow_id_prefix="run",
    )
    configure_run_actor_dependencies(deps)
    gateway = LocalWorkflowGateway(deps)
    return KernelFacade(workflow_gateway=gateway, substrate_type="local_fsm")


# ---------------------------------------------------------------------------
# Benchmark: concurrent run starts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_run_starts(benchmark_run_count: int = 100) -> None:
    """Start N runs concurrently and verify all get unique run_ids."""
    kernel = _make_kernel()

    requests = [
        StartRunRequest(
            run_kind=f"bench-{i}",
            initiator="system",
            input_json={"run_id": f"concurrent-start-{i}"},
        )
        for i in range(benchmark_run_count)
    ]

    t0 = time.perf_counter()
    responses = await asyncio.gather(
        *(kernel.start_run(req) for req in requests),
    )
    elapsed = time.perf_counter() - t0

    run_ids = [r.run_id for r in responses]
    assert len(run_ids) == benchmark_run_count
    assert len(set(run_ids)) == benchmark_run_count, "All run_ids must be unique"

    for resp in responses:
        assert resp.lifecycle_state == "created"

    print(
        f"\n[benchmark] test_concurrent_run_starts:"
        f" {benchmark_run_count} runs in {elapsed:.4f}s"
        f" ({benchmark_run_count / elapsed:.0f} runs/s)"
    )


# ---------------------------------------------------------------------------
# Benchmark: concurrent signal delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_signal_delivery(benchmark_run_count: int = 50) -> None:
    """Start N runs, then send signals to all concurrently."""
    kernel = _make_kernel()

    # Start runs sequentially to ensure they all exist before signalling.
    run_ids: list[str] = []
    for i in range(benchmark_run_count):
        resp = await kernel.start_run(
            StartRunRequest(
                run_kind=f"sig-{i}",
                initiator="system",
                input_json={"run_id": f"signal-bench-{i}"},
            )
        )
        run_ids.append(resp.run_id)

    signals = [
        SignalRunRequest(
            run_id=rid,
            signal_type="heartbeat",
            signal_payload={"seq": idx},
        )
        for idx, rid in enumerate(run_ids)
    ]

    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(kernel.signal_run(sig) for sig in signals),
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - t0

    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 0, f"Signal failures: {failures}"

    print(
        f"\n[benchmark] test_concurrent_signal_delivery:"
        f" {benchmark_run_count} signals in {elapsed:.4f}s"
        f" ({benchmark_run_count / elapsed:.0f} signals/s)"
    )


# ---------------------------------------------------------------------------
# Benchmark: concurrent queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_queries(benchmark_run_count: int = 100) -> None:
    """Start runs, then issue N concurrent query_run() calls."""
    kernel = _make_kernel()

    run_ids: list[str] = []
    for i in range(benchmark_run_count):
        resp = await kernel.start_run(
            StartRunRequest(
                run_kind=f"query-{i}",
                initiator="system",
                input_json={"run_id": f"query-bench-{i}"},
            )
        )
        run_ids.append(resp.run_id)

    queries = [QueryRunRequest(run_id=rid) for rid in run_ids]

    t0 = time.perf_counter()
    responses = await asyncio.gather(
        *(kernel.query_run(q) for q in queries),
    )
    elapsed = time.perf_counter() - t0

    for resp in responses:
        assert resp.run_id in run_ids
        assert resp.projected_offset >= 0

    print(
        f"\n[benchmark] test_concurrent_queries:"
        f" {benchmark_run_count} queries in {elapsed:.4f}s"
        f" ({benchmark_run_count / elapsed:.0f} queries/s)"
    )


# ---------------------------------------------------------------------------
# Benchmark: sequential vs concurrent speedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_vs_concurrent_speedup() -> None:
    """Compare sequential start_run vs concurrent start_run for 50 runs."""
    count = 50

    # --- Sequential ---
    kernel_seq = _make_kernel()
    t0 = time.perf_counter()
    for i in range(count):
        await kernel_seq.start_run(
            StartRunRequest(
                run_kind=f"seq-{i}",
                initiator="system",
                input_json={"run_id": f"seq-run-{i}"},
            )
        )
    sequential_elapsed = time.perf_counter() - t0

    # --- Concurrent ---
    kernel_conc = _make_kernel()
    requests = [
        StartRunRequest(
            run_kind=f"conc-{i}",
            initiator="system",
            input_json={"run_id": f"conc-run-{i}"},
        )
        for i in range(count)
    ]
    t0 = time.perf_counter()
    await asyncio.gather(*(kernel_conc.start_run(req) for req in requests))
    concurrent_elapsed = time.perf_counter() - t0

    print(
        f"\n[benchmark] test_sequential_vs_concurrent_speedup:"
        f"\n  sequential: {sequential_elapsed:.4f}s"
        f"\n  concurrent: {concurrent_elapsed:.4f}s"
        f"\n  speedup:    {sequential_elapsed / concurrent_elapsed:.2f}x"
    )

    # Concurrent should not be significantly slower than sequential.
    # With in-memory substrate both are fast, but concurrent must not regress.
    assert concurrent_elapsed <= sequential_elapsed * 1.5, (
        f"Concurrent ({concurrent_elapsed:.4f}s) is unexpectedly slower"
        f" than sequential ({sequential_elapsed:.4f}s)"
    )


# ---------------------------------------------------------------------------
# Benchmark: projection consistency under concurrent signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_consistency_under_concurrent_signals() -> None:
    """Send 20 concurrent signals to a single run, verify no events are lost."""
    kernel = _make_kernel()
    signal_count = 20

    resp = await kernel.start_run(
        StartRunRequest(
            run_kind="consistency",
            initiator="system",
            input_json={"run_id": "consistency-run"},
        )
    )
    run_id = resp.run_id

    signals = [
        SignalRunRequest(
            run_id=run_id,
            signal_type="tick",
            signal_payload={"seq": i},
        )
        for i in range(signal_count)
    ]

    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(kernel.signal_run(sig) for sig in signals),
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - t0

    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 0, f"Signal failures: {failures}"

    projection = await kernel.query_run(QueryRunRequest(run_id=run_id))
    assert projection.run_id == run_id

    # The projected_offset must reflect all signals were received.
    # Each signal should have advanced the projection offset by at least 1.
    assert projection.projected_offset >= signal_count, (
        f"Expected projected_offset >= {signal_count},"
        f" got {projection.projected_offset} -- events may have been lost"
    )

    print(
        f"\n[benchmark] test_projection_consistency_under_concurrent_signals:"
        f"\n  {signal_count} concurrent signals in {elapsed:.4f}s"
        f"\n  final projected_offset: {projection.projected_offset}"
    )
