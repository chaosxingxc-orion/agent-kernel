import type {
  ActionCommit,
  CommitBatchRef,
  EventLog,
  RuntimeEvent,
} from './event_types.js';

/** Stores runtime events in memory for local and PoC scenarios. */
export class InMemoryEventLog implements EventLog {
  private readonly eventsByRun: Map<string, RuntimeEvent[]> = new Map();
  private readonly nextOffsetByRun: Map<string, number> = new Map();

  /** Appends one action commit and returns its batch reference. */
  public async appendActionCommit(
    commit: ActionCommit,
  ): Promise<CommitBatchRef> {
    const runEvents: RuntimeEvent[] = this.eventsByRun.get(commit.runId) ?? [];
    const nextOffset: number = this.nextOffsetByRun.get(commit.runId) ?? 1;
    let currentOffset = nextOffset;

    const normalizedEvents: RuntimeEvent[] = commit.events.map((event) => {
      const normalizedEvent: RuntimeEvent = {
        ...event,
        commitOffset: currentOffset,
      };
      currentOffset += 1;
      return normalizedEvent;
    });

    runEvents.push(...normalizedEvents);
    this.eventsByRun.set(commit.runId, runEvents);
    this.nextOffsetByRun.set(commit.runId, currentOffset);

    return {
      commitId: commit.commitId,
      runId: commit.runId,
      throughOffset: currentOffset - 1,
      eventIds: normalizedEvents.map((event) => event.eventId),
    };
  }

  /** Loads ordered events for one run after an optional offset. */
  public async load(runId: string, afterOffset = 0): Promise<RuntimeEvent[]> {
    const runEvents: RuntimeEvent[] = this.eventsByRun.get(runId) ?? [];
    return runEvents.filter((event) => event.commitOffset > afterOffset);
  }
}
