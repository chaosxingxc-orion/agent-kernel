import { describe, expect, it, vi } from 'vitest';
import { RunActorRuntime } from '../../../src/core/actor/run_actor.js';
import {
  RunLifecycleState,
  type RunMailboxBatch,
} from '../../../src/core/eventlog/event_types.js';

function makeBatch(throughOffset: number): RunMailboxBatch {
  return {
    runId: 'run-1',
    refs: [
      {
        commitId: `commit-${throughOffset}`,
        runId: 'run-1',
        throughOffset,
        eventIds: [`evt-${throughOffset}`],
      },
    ],
    maxThroughOffset: throughOffset,
    wakeReasons: ['action_commit'],
  };
}

function createDeferred(): { promise: Promise<void>; resolve: () => void } {
  let resolveValue: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    resolveValue = resolve;
  });
  return {
    promise,
    resolve: () => resolveValue?.(),
  };
}

describe('RunActorRuntime', () => {
  it('coalesces same-run signals into one batch using maxThroughOffset', async () => {
    const runtime = new RunActorRuntime(
      { appendActionCommit: vi.fn(), load: vi.fn() },
      { catchUp: vi.fn(), readiness: vi.fn(), get: vi.fn() },
      { admit: vi.fn() },
      { execute: vi.fn() },
      { decide: vi.fn() },
    );

    await runtime.signal('run-1', makeBatch(3).refs[0]);
    await runtime.signal('run-1', makeBatch(7).refs[0]);
    const batch = await runtime.dequeueBatch('run-1');

    expect(batch).not.toBeNull();
    expect(batch?.maxThroughOffset).toBe(7);
    expect(batch?.refs).toHaveLength(2);
  });

  it('serializes authoritative decision rounds for the same run', async () => {
    let concurrent = 0;
    let maxConcurrent = 0;
    const gate = createDeferred();

    const runtime = new RunActorRuntime(
      { appendActionCommit: vi.fn(), load: vi.fn() },
      {
        catchUp: vi.fn(async () => {
          concurrent += 1;
          maxConcurrent = Math.max(maxConcurrent, concurrent);
          await gate.promise;
          concurrent -= 1;
          return {
            runId: 'run-1',
            lifecycleState: RunLifecycleState.Ready,
            waitingExternal: false,
            readyForDispatch: true,
            projectedOffset: 3,
          };
        }),
        readiness: vi.fn(async () => ({ ready: false, requiredOffset: 3 })),
        get: vi.fn(),
      },
      { admit: vi.fn() },
      { execute: vi.fn() },
      { decide: vi.fn() },
    );

    const first = runtime.processBatch('run-1', makeBatch(3));
    const second = runtime.processBatch('run-1', makeBatch(4));
    await Promise.resolve();
    gate.resolve();
    await Promise.allSettled([first, second]);

    expect(maxConcurrent).toBe(1);
  });

  it('never executes without successful admission', async () => {
    const execute = vi.fn();
    const admit = vi.fn(async () => ({
      admitted: false,
      reasonCode: 'expired_action',
    }));
    const runtime = new RunActorRuntime(
      { appendActionCommit: vi.fn(), load: vi.fn() },
      {
        catchUp: vi.fn(async () => ({
          runId: 'run-1',
          lifecycleState: RunLifecycleState.Ready,
          waitingExternal: false,
          readyForDispatch: true,
          projectedOffset: 7,
          nextAction: {
            runId: 'run-1',
            actionId: 'action-1',
            actionType: 'idempotent_write',
            payload: {},
          },
          dispatchArtifact: {
            artifactKey: 'artifact-1',
            manifest: { actionType: 'idempotent_write' },
            resolvedHandler: 'handler',
            permissionTemplate: [],
            staticDependencyGraph: [],
            compileVersion: 'v1',
          },
        })),
        readiness: vi.fn(async () => ({ ready: true, requiredOffset: 7 })),
        get: vi.fn(),
      },
      { admit },
      { execute },
      { decide: vi.fn() },
    );

    await runtime.processBatch('run-1', makeBatch(7));

    expect(admit).toHaveBeenCalledOnce();
    expect(execute).not.toHaveBeenCalled();
  });

  it('passes explicit offsets through catchUp and readiness before dispatch', async () => {
    const calls: string[] = [];
    const runtime = new RunActorRuntime(
      { appendActionCommit: vi.fn(), load: vi.fn() },
      {
        catchUp: vi.fn(async (_runId, throughOffset) => {
          calls.push(`catchUp:${throughOffset}`);
          return {
            runId: 'run-1',
            lifecycleState: RunLifecycleState.Completed,
            waitingExternal: false,
            readyForDispatch: false,
            projectedOffset: throughOffset,
          };
        }),
        readiness: vi.fn(async (_runId, requiredOffset) => {
          calls.push(`readiness:${requiredOffset}`);
          return { ready: false, requiredOffset };
        }),
        get: vi.fn(),
      },
      { admit: vi.fn() },
      { execute: vi.fn() },
      { decide: vi.fn() },
    );

    await runtime.processBatch('run-1', makeBatch(11));

    expect(calls).toEqual(['catchUp:11', 'readiness:11']);
  });
});
