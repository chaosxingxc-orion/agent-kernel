import type {
  Admission,
  DecisionProjection,
  EventLog,
  Executor,
  Recovery,
  RunMailboxBatch,
} from '../eventlog/event_types.js';

/** Hosts same-run orchestration and delegates authoritative services. */
export class RunActorRuntime {
  private readonly mailboxByRun: Map<string, RunMailboxBatch['refs']> = new Map();
  private readonly runQueueByRun: Map<string, Promise<void>> = new Map();

  /** Constructs one run actor runtime instance with kernel dependencies. */
  public constructor(
    private readonly _eventLog: EventLog,
    private readonly _projection: DecisionProjection,
    private readonly _admission: Admission,
    private readonly _executor: Executor,
    private readonly _recovery: Recovery,
  ) {}

  /** Accepts one commit reference signal for the target run mailbox. */
  public async signal(
    runId: string,
    ref: RunMailboxBatch['refs'][number],
  ): Promise<void> {
    const refs: RunMailboxBatch['refs'] = this.mailboxByRun.get(runId) ?? [];
    refs.push(ref);
    this.mailboxByRun.set(runId, refs);
  }

  /** Dequeues one coalesced mailbox batch for the target run. */
  public async dequeueBatch(runId: string): Promise<RunMailboxBatch | null> {
    const refs: RunMailboxBatch['refs'] | undefined = this.mailboxByRun.get(runId);
    if (!refs || refs.length === 0) {
      return null;
    }
    this.mailboxByRun.delete(runId);
    const maxThroughOffset: number = refs.reduce(
      (currentMax, currentRef) => Math.max(currentMax, currentRef.throughOffset),
      0,
    );
    return {
      runId,
      refs: [...refs],
      maxThroughOffset,
      wakeReasons: ['action_commit'],
    };
  }

  /** Processes one mailbox batch through catch-up, admission, and execution. */
  public async processBatch(
    runId: string,
    batch: RunMailboxBatch,
  ): Promise<void> {
    const previousTask: Promise<void> = this.runQueueByRun.get(runId) ?? Promise.resolve();
    const currentTask: Promise<void> = previousTask.then(
      async () => this.processBatchOnce(runId, batch),
    );
    // Keep the per-run queue alive even if one task fails.
    this.runQueueByRun.set(
      runId,
      currentTask.catch(() => undefined),
    );
    return currentTask;
  }

  /** Runs one authoritative decision round for one already-serialized batch. */
  private async processBatchOnce(
    runId: string,
    batch: RunMailboxBatch,
  ): Promise<void> {
    const projection = await this._projection.catchUp(runId, batch.maxThroughOffset);
    const readiness = await this._projection.readiness(runId, batch.maxThroughOffset);
    if (!readiness.ready) {
      return;
    }
    if (!projection.nextAction || !projection.dispatchArtifact) {
      return;
    }

    const admission = await this._admission.admit(
      projection.nextAction,
      projection.dispatchArtifact,
    );
    if (!admission.admitted) {
      return;
    }
    await this._executor.execute(projection.nextAction);
  }
}
