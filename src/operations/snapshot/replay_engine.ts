import type {
  RuntimeEvent,
  RunProjection,
} from '../../core/eventlog/event_types.js';
import type {
  ProjectionCursor,
  SnapshotCompatibilityPolicy,
  SnapshotMeta,
} from './versioning.js';

/** Bundles snapshot metadata and the associated projection payload. */
export interface ProjectionSnapshot {
  meta: SnapshotMeta;
  projection: RunProjection;
}

/** Declares all external dependencies used by replay recovery. */
export interface ReplayDependencies {
  loadLatestSnapshot(runId: string): Promise<ProjectionSnapshot | null>;
  loadCursor(runId: string): Promise<ProjectionCursor | null>;
  loadEventsAfter(runId: string, offset: number): Promise<RuntimeEvent[]>;
  genesisProjection(runId: string): Promise<RunProjection>;
  applyEvents(base: RunProjection, events: RuntimeEvent[]): Promise<RunProjection>;
}

/** Rebuilds projection state from snapshot and append-only event history. */
export class ReplayEngine {
  /** Constructs one replay engine with dependencies and compatibility policy. */
  public constructor(
    private readonly _deps: ReplayDependencies,
    private readonly _compatibility: SnapshotCompatibilityPolicy,
  ) {}

  /** Recovers the latest projection for one run. */
  public async recover(runId: string): Promise<RunProjection> {
    const latestSnapshot = await this._deps.loadLatestSnapshot(runId);
    const compatibleSnapshot = this.resolveCompatibleSnapshot(latestSnapshot);
    const baseProjection = compatibleSnapshot
      ? compatibleSnapshot.projection
      : await this._deps.genesisProjection(runId);
    const replayAfterOffset = compatibleSnapshot
      ? Math.max(
        compatibleSnapshot.meta.throughOffset,
        compatibleSnapshot.projection.projectedOffset,
      )
      : await this.resolveCursorOffset(runId, baseProjection.projectedOffset);
    const events = await this._deps.loadEventsAfter(runId, replayAfterOffset);
    return this._deps.applyEvents(baseProjection, events);
  }

  /** Returns compatible snapshot or null when recovery must fall back. */
  private resolveCompatibleSnapshot(
    snapshot: ProjectionSnapshot | null,
  ): ProjectionSnapshot | null {
    if (!snapshot) {
      return null;
    }
    const schemaCompatible = this._compatibility.isSnapshotSchemaCompatible(
      snapshot.meta.snapshotSchemaVersion,
    );
    const projectorCompatible = this._compatibility.isProjectorCodeCompatible(
      snapshot.meta.projectorCodeVersion,
    );
    if (!schemaCompatible || !projectorCompatible) {
      return null;
    }
    return snapshot;
  }

  /** Resolves cursor offset with projection offset as deterministic fallback. */
  private async resolveCursorOffset(
    runId: string,
    projectionOffset: number,
  ): Promise<number> {
    const cursor = await this._deps.loadCursor(runId);
    if (!cursor) {
      return projectionOffset;
    }
    return Math.max(cursor.lastSeenOffset, projectionOffset);
  }
}
