import { describe, expect, it, vi } from 'vitest';
import {
  RunLifecycleState,
  type RuntimeEvent,
} from '../../../src/core/eventlog/event_types.js';
import { ReplayEngine } from '../../../src/operations/snapshot/replay_engine.js';

function makeEvent(offset: number): RuntimeEvent {
  return {
    eventId: `evt-${offset}`,
    runId: 'run-1',
    eventType: 'action_admitted',
    eventClass: 'derived',
    eventAuthority: 'derived_replayable',
    orderingKey: 'run-1',
    commitOffset: offset,
    eventTime: '2026-03-30T00:00:00Z',
    wakePolicy: 'wake_harness',
  };
}

describe('ReplayEngine', () => {
  it('recovers from the latest compatible snapshot and replays after the max offset', async () => {
    const loadEventsAfter = vi.fn(async () => [makeEvent(11), makeEvent(12)]);
    const engine = new ReplayEngine(
      {
        loadLatestSnapshot: vi.fn(async () => ({
          meta: {
            snapshotId: 'snapshot-1',
            runId: 'run-1',
            throughOffset: 10,
            snapshotSchemaVersion: '1',
            projectorCodeVersion: '1',
            createdAt: '2026-03-30T00:00:00Z',
          },
          projection: {
            runId: 'run-1',
            lifecycleState: RunLifecycleState.Ready,
            waitingExternal: false,
            readyForDispatch: true,
            projectedOffset: 10,
          },
        })),
        loadCursor: vi.fn(async () => ({
          runId: 'run-1',
          projectorId: 'decision',
          projectorEpoch: 1,
          lastSeenEventId: 'evt-10',
          lastSeenOffset: 9,
          updatedAt: '2026-03-30T00:00:00Z',
        })),
        loadEventsAfter,
        genesisProjection: vi.fn(),
        applyEvents: vi.fn(async (base, events) => ({
          ...base,
          projectedOffset: events.at(-1)?.commitOffset ?? base.projectedOffset,
        })),
      },
      {
        isSnapshotSchemaCompatible: vi.fn(() => true),
        isProjectorCodeCompatible: vi.fn(() => true),
      },
    );

    const projection = await engine.recover('run-1');

    expect(loadEventsAfter).toHaveBeenCalledWith('run-1', 10);
    expect(projection.projectedOffset).toBe(12);
  });

  it('falls back to genesis replay when the latest snapshot is incompatible', async () => {
    const genesisProjection = vi.fn(async () => ({
      runId: 'run-1',
      lifecycleState: RunLifecycleState.Created,
      waitingExternal: false,
      readyForDispatch: false,
      projectedOffset: 0,
    }));

    const engine = new ReplayEngine(
      {
        loadLatestSnapshot: vi.fn(async () => ({
          meta: {
            snapshotId: 'snapshot-1',
            runId: 'run-1',
            throughOffset: 10,
            snapshotSchemaVersion: 'old',
            projectorCodeVersion: 'old',
            createdAt: '2026-03-30T00:00:00Z',
          },
          projection: {
            runId: 'run-1',
            lifecycleState: RunLifecycleState.Ready,
            waitingExternal: false,
            readyForDispatch: true,
            projectedOffset: 10,
          },
        })),
        loadCursor: vi.fn(async () => null),
        loadEventsAfter: vi.fn(async () => [makeEvent(1), makeEvent(2)]),
        genesisProjection,
        applyEvents: vi.fn(async (base, events) => ({
          ...base,
          projectedOffset: events.at(-1)?.commitOffset ?? base.projectedOffset,
        })),
      },
      {
        isSnapshotSchemaCompatible: vi.fn(() => false),
        isProjectorCodeCompatible: vi.fn(() => false),
      },
    );

    const projection = await engine.recover('run-1');

    expect(genesisProjection).toHaveBeenCalledWith('run-1');
    expect(projection.projectedOffset).toBe(2);
  });

  it('recovers without any snapshot by using cursor plus replay only', async () => {
    const engine = new ReplayEngine(
      {
        loadLatestSnapshot: vi.fn(async () => null),
        loadCursor: vi.fn(async () => ({
          runId: 'run-1',
          projectorId: 'decision',
          projectorEpoch: 1,
          lastSeenEventId: 'evt-5',
          lastSeenOffset: 5,
          updatedAt: '2026-03-30T00:00:00Z',
        })),
        loadEventsAfter: vi.fn(async () => [makeEvent(6), makeEvent(7)]),
        genesisProjection: vi.fn(async () => ({
          runId: 'run-1',
          lifecycleState: RunLifecycleState.Created,
          waitingExternal: false,
          readyForDispatch: false,
          projectedOffset: 0,
        })),
        applyEvents: vi.fn(async (base, events) => ({
          ...base,
          projectedOffset: events.at(-1)?.commitOffset ?? base.projectedOffset,
        })),
      },
      {
        isSnapshotSchemaCompatible: vi.fn(() => true),
        isProjectorCodeCompatible: vi.fn(() => true),
      },
    );

    const projection = await engine.recover('run-1');

    expect(projection.projectedOffset).toBe(7);
  });
});
