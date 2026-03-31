import type {
  DecisionProjection,
  DecisionReadiness,
  RunProjection,
} from '../eventlog/event_types.js';
import { RunLifecycleState } from '../eventlog/event_types.js';

/** Provides an in-memory decision projection for PoC and tests. */
export class InMemoryDecisionProjection implements DecisionProjection {
  private readonly projectionByRun: Map<string, RunProjection> = new Map();

  /** Catches up one run projection through the given offset. */
  public async catchUp(
    runId: string,
    throughOffset: number,
  ): Promise<RunProjection> {
    const current = await this.get(runId);
    const nextProjection: RunProjection = {
      ...current,
      projectedOffset: Math.max(current.projectedOffset, throughOffset),
    };
    this.projectionByRun.set(runId, nextProjection);
    return nextProjection;
  }

  /** Evaluates whether projection is ready at the required offset. */
  public async readiness(
    runId: string,
    requiredOffset: number,
  ): Promise<DecisionReadiness> {
    const projection = await this.get(runId);
    return {
      ready: projection.projectedOffset >= requiredOffset,
      requiredOffset,
    };
  }

  /** Returns the latest projection snapshot for one run. */
  public async get(runId: string): Promise<RunProjection> {
    const projection = this.projectionByRun.get(runId);
    if (projection) {
      return projection;
    }
    const initialProjection: RunProjection = {
      runId,
      lifecycleState: RunLifecycleState.Created,
      waitingExternal: false,
      readyForDispatch: false,
      projectedOffset: 0,
    };
    this.projectionByRun.set(runId, initialProjection);
    return initialProjection;
  }
}
