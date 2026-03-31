import { describe, expect, it, vi } from 'vitest';
import { RunActorRuntime } from '../../src/core/actor/run_actor.js';
import { InMemoryEventLog } from '../../src/core/eventlog/event_log.js';
import {
  RunLifecycleState,
  type ActionEnvelope,
  type DecisionProjection,
  type DecisionReadiness,
  type RunProjection,
} from '../../src/core/eventlog/event_types.js';
import { RunLifecycleManager } from '../../src/operations/lifecycle/run_lifecycle.js';
import type {
  ProjectionSnapshot,
} from '../../src/operations/snapshot/replay_engine.js';
import { ReplayEngine } from '../../src/operations/snapshot/replay_engine.js';

class ReplayBackedDecisionProjection implements DecisionProjection {
  private readonly byRun: Map<string, RunProjection> = new Map();

  public constructor(
    private readonly replay: ReplayEngine,
    private readonly lifecycle: RunLifecycleManager,
  ) {}

  public async catchUp(runId: string, throughOffset: number): Promise<RunProjection> {
    const recovered = await this.replay.recover(runId);
    const canDispatch = this.lifecycle.canDispatch(recovered.lifecycleState);
    const nextAction = canDispatch ? this.buildNextAction(runId, throughOffset) : undefined;
    const projection: RunProjection = {
      ...recovered,
      projectedOffset: Math.max(recovered.projectedOffset, throughOffset),
      readyForDispatch: canDispatch,
      nextAction,
      dispatchArtifact: nextAction
        ? {
          artifactKey: 'artifact-dispatch',
          manifest: { actionType: nextAction.actionType },
          resolvedHandler: 'handler.dispatch',
          permissionTemplate: [],
          staticDependencyGraph: [],
          compileVersion: 'v1',
        }
        : undefined,
    };
    this.byRun.set(runId, projection);
    return projection;
  }

  public async readiness(runId: string, requiredOffset: number): Promise<DecisionReadiness> {
    const projection = await this.get(runId);
    return {
      ready: projection.projectedOffset >= requiredOffset,
      requiredOffset,
    };
  }

  public async get(runId: string): Promise<RunProjection> {
    const existing = this.byRun.get(runId);
    if (existing) {
      return existing;
    }
    const initial: RunProjection = {
      runId,
      lifecycleState: RunLifecycleState.Created,
      waitingExternal: false,
      readyForDispatch: false,
      projectedOffset: 0,
    };
    this.byRun.set(runId, initial);
    return initial;
  }

  private buildNextAction(runId: string, projectedOffset: number): ActionEnvelope {
    return {
      runId,
      actionId: `action-${projectedOffset}`,
      actionType: 'idempotent_write',
      payload: { projectedOffset },
    };
  }
}

describe('Cross-module collaboration', () => {
  it('composes event log + replay + lifecycle + actor runtime in one decision round', async () => {
    const runId = 'run-integration-1';
    const eventLog = new InMemoryEventLog();
    const lifecycle = new RunLifecycleManager();

    const lifecycleTransition = await lifecycle.transition(
      RunLifecycleState.Created,
      'external_event_received',
      runId,
    );

    const commitRef = await eventLog.appendActionCommit({
      commitId: 'commit-integration-1',
      runId,
      actionId: 'action-bootstrap',
      events: lifecycleTransition.events,
    });

    const snapshots: Map<string, ProjectionSnapshot> = new Map();
    snapshots.set(runId, {
      meta: {
        snapshotId: 'snapshot-bootstrap',
        runId,
        throughOffset: 0,
        snapshotSchemaVersion: '1',
        projectorCodeVersion: '1',
        createdAt: '2026-03-31T00:00:00Z',
      },
      projection: {
        runId,
        lifecycleState: RunLifecycleState.Created,
        waitingExternal: false,
        readyForDispatch: false,
        projectedOffset: 0,
      },
    });

    const replay = new ReplayEngine(
      {
        loadLatestSnapshot: async (requestedRunId) => snapshots.get(requestedRunId) ?? null,
        loadCursor: async () => null,
        loadEventsAfter: async (requestedRunId, offset) => eventLog.load(requestedRunId, offset),
        genesisProjection: async (requestedRunId) => ({
          runId: requestedRunId,
          lifecycleState: RunLifecycleState.Created,
          waitingExternal: false,
          readyForDispatch: false,
          projectedOffset: 0,
        }),
        applyEvents: async (base, events) => {
          let current = { ...base };
          for (const event of events) {
            if (event.eventType === 'run_lifecycle_transitioned') {
              const nextState = event.payload?.nextState;
              if (nextState && typeof nextState === 'string') {
                current = {
                  ...current,
                  lifecycleState: nextState as RunLifecycleState,
                  readyForDispatch: lifecycle.canDispatch(nextState as RunLifecycleState),
                  waitingExternal: nextState === RunLifecycleState.WaitingExternal,
                };
              }
            }
            current = {
              ...current,
              projectedOffset: Math.max(current.projectedOffset, event.commitOffset),
            };
          }
          return current;
        },
      },
      {
        isSnapshotSchemaCompatible: () => true,
        isProjectorCodeCompatible: () => true,
      },
    );

    const projection = new ReplayBackedDecisionProjection(replay, lifecycle);
    const execute = vi.fn(async () => ({
      factEvents: [],
      derivedEvents: [],
    }));
    const admit = vi.fn(async () => ({
      admitted: true,
      reasonCode: 'ok',
    }));

    const runtime = new RunActorRuntime(
      eventLog,
      projection,
      { admit },
      { execute },
      { decide: vi.fn() },
    );

    await runtime.signal(runId, commitRef);
    const batch = await runtime.dequeueBatch(runId);

    expect(batch).not.toBeNull();
    expect(batch?.maxThroughOffset).toBe(commitRef.throughOffset);

    await runtime.processBatch(runId, batch!);

    expect(admit).toHaveBeenCalledOnce();
    expect(execute).toHaveBeenCalledOnce();
    expect(execute).toHaveBeenCalledWith(
      expect.objectContaining({
        runId,
        actionType: 'idempotent_write',
      }),
    );

    const finalProjection = await projection.get(runId);
    expect(finalProjection.lifecycleState).toBe(RunLifecycleState.Ready);
    expect(finalProjection.projectedOffset).toBe(commitRef.throughOffset);
  });
});
