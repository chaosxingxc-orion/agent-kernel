"""Tests for ScheduledOutboxReconciler periodic reconciliation scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from agent_kernel.kernel.persistence.dispatch_outbox_reconciler import (
    ReconciliationResult,
    ScheduledOutboxReconciler,
)


@dataclass
class _ReconcilerStub:
    """Stub reconciler that returns configurable per-run results."""

    per_run_found: dict[str, int]
    per_run_repaired: dict[str, int]

    async def reconcile(
        self,
        _event_log: object,
        _dedupe_store: object,
        run_id: str,
    ) -> ReconciliationResult:
        return ReconciliationResult(
            run_id=run_id,
            violations_found=self.per_run_found.get(run_id, 0),
            violations_repaired=self.per_run_repaired.get(run_id, 0),
        )


class _EventLogWithRuns:
    def __init__(self, run_ids: list[str]) -> None:
        self._events_by_run = {run_id: [] for run_id in run_ids}


@pytest.mark.asyncio
async def test_reconcile_once_aggregates_counts_from_provider() -> None:
    reconciler = _ReconcilerStub(
        per_run_found={"r1": 2, "r2": 3},
        per_run_repaired={"r1": 1, "r2": 2},
    )
    scheduler = ScheduledOutboxReconciler(
        reconciler=reconciler,  # type: ignore[arg-type]
        event_log=object(),
        dedupe_store=object(),
        run_ids_provider=lambda: ["r1", "r2"],
    )
    result = await scheduler.reconcile_once()
    assert result.scanned_run_ids == ["r1", "r2"]
    assert result.violations_found == 5
    assert result.violations_repaired == 3
    assert scheduler.last_reconciliation_result is result


@pytest.mark.asyncio
async def test_reconcile_once_discovers_run_ids_from_event_log() -> None:
    reconciler = _ReconcilerStub(per_run_found={"r1": 1}, per_run_repaired={"r1": 1})
    scheduler = ScheduledOutboxReconciler(
        reconciler=reconciler,  # type: ignore[arg-type]
        event_log=_EventLogWithRuns(["r1", "r2"]),
        dedupe_store=object(),
    )
    result = await scheduler.reconcile_once()
    assert set(result.scanned_run_ids) == {"r1", "r2"}
    assert result.violations_found == 1
    assert result.violations_repaired == 1


@pytest.mark.asyncio
async def test_reconcile_once_emits_observability_on_violations() -> None:
    hook = MagicMock()
    reconciler = _ReconcilerStub(per_run_found={"r1": 1}, per_run_repaired={"r1": 0})
    scheduler = ScheduledOutboxReconciler(
        reconciler=reconciler,  # type: ignore[arg-type]
        event_log=object(),
        dedupe_store=object(),
        run_ids_provider=lambda: ["r1"],
        observability_hook=hook,
    )
    await scheduler.reconcile_once()
    hook.on_recovery_triggered.assert_called_once()


@pytest.mark.asyncio
async def test_start_is_idempotent_and_task_is_cancellable() -> None:
    reconciler = _ReconcilerStub(per_run_found={}, per_run_repaired={})
    scheduler = ScheduledOutboxReconciler(
        reconciler=reconciler,  # type: ignore[arg-type]
        event_log=object(),
        dedupe_store=object(),
        run_ids_provider=lambda: [],
        interval_s=60.0,
    )
    task_1 = scheduler.start()
    task_2 = scheduler.start()
    assert task_1 is task_2
    task_1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_1
