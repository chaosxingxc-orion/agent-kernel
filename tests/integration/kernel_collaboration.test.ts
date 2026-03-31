import { describe, expect, it, vi } from 'vitest';
import { RunActorRuntime } from '../../src/core/actor/run_actor.js';
import { InMemoryEventLog } from '../../src/core/eventlog/event_log.js';
import {
  RunLifecycleState,
  type ActionCommit,
  type RuntimeEvent,
} from '../../src/core/eventlog/event_types.js';
import { RunLifecycleManager } from '../../src/operations/lifecycle/run_lifecycle.js';
import { ReplayEngine } from '../../src/operations/snapshot/replay_engine.js';

function buildFactEvent(
  runId: string,
  eventId: string,
  eventType: string,
): RuntimeEvent {
  return {
    eventId,
    runId,
    eventType,
    eventClass: 'fact',
    eventAuthority: 'authoritative_fact',
    orderingKey: runId,
    commitOffset: 0,
    eventTime: '2026-04-01T00:00:00Z',
    wakePolicy: 'wake_harness',
  };
}

describe('Kernel Collaboration Integration', () => {
  it('coordinates eventlog, run actor, lifecycle, and replay in one flow', async () => {
    const eventLog = new InMemoryEventLog();
    const executor = vi.fn(async () => ({
      factEvents: [],
      derivedEvents: [],
    }));
    const runActor = new RunActorRuntime(
      eventLog,
      {
        catchUp: vi.fn(async () => ({
          runId: 'run-int-1',
          lifecycleState: RunLifecycleState.Ready,
          waitingExternal: false,
          readyForDispatch: true,
          projectedOffset: 2,
          nextAction: {
            runId: 'run-int-1',
            actionId: 'action-1',
            actionType: 'tool.search',
            payload: { query: 'kernel' },
          },
          dispatchArtifact: {
            artifactKey: 'artifact-1',
            manifest: { actionType: 'tool.search' },
            resolvedHandler: 'handler.search',
            permissionTemplate: [],
            staticDependencyGraph: [],
            compileVersion: 'v1',
          },
        })),
        readiness: vi.fn(async () => ({ ready: true, requiredOffset: 2 })),
        get: vi.fn(),
      },
      {
        admit: vi.fn(async () => ({
          admitted: true,
          reasonCode: 'ok',
        })),
      },
      { execute: executor },
      { decide: vi.fn() },
    );

    const actionCommit: ActionCommit = {
      commitId: 'commit-action-1',
      runId: 'run-int-1',
      actionId: 'action-1',
      events: [buildFactEvent('run-int-1', 'evt-action-1', 'action_committed')],
    };
    const commitRef = await eventLog.appendActionCommit(actionCommit);
    await runActor.signal('run-int-1', commitRef);
    const batch = await runActor.dequeueBatch('run-int-1');
    expect(batch).not.toBeNull();
    await runActor.processBatch('run-int-1', batch!);

    expect(executor).toHaveBeenCalledOnce();

    const lifecycle = new RunLifecycleManager();
    const transition = await lifecycle.transition(
      RunLifecycleState.Ready,
      'action_admitted',
      'run-int-1',
    );
    await eventLog.appendActionCommit({
      commitId: 'commit-lifecycle-1',
      runId: 'run-int-1',
      actionId: 'action-1',
      events: transition.events,
    });

    const replay = new ReplayEngine(
      {
        loadLatestSnapshot: async () => null,
        loadCursor: async () => null,
        loadEventsAfter: async (runId, offset) => eventLog.load(runId, offset),
        genesisProjection: async (runId) => ({
          runId,
          lifecycleState: RunLifecycleState.Created,
          waitingExternal: false,
          readyForDispatch: false,
          projectedOffset: 0,
        }),
        applyEvents: async (base, events) => ({
          ...base,
          lifecycleState: transition.nextState,
          projectedOffset: events.at(-1)?.commitOffset ?? base.projectedOffset,
        }),
      },
      {
        isSnapshotSchemaCompatible: () => true,
        isProjectorCodeCompatible: () => true,
      },
    );
    const recovered = await replay.recover('run-int-1');
    expect(recovered.lifecycleState).toBe(RunLifecycleState.WaitingResult);
    expect(recovered.projectedOffset).toBeGreaterThan(0);
  });
});

