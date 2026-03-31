/** Describes persisted metadata for one projection snapshot artifact. */
export interface SnapshotMeta {
  snapshotId: string;
  runId: string;
  throughOffset: number;
  snapshotSchemaVersion: string;
  projectorCodeVersion: string;
  createdAt: string;
}

/** Describes the projector cursor used by replay catch-up flows. */
export interface ProjectionCursor {
  runId: string;
  projectorId: string;
  projectorEpoch: number;
  lastSeenEventId: string;
  lastSeenOffset: number;
  updatedAt: string;
}

/** Evaluates whether a snapshot and projector pair is replay-compatible. */
export interface SnapshotCompatibilityPolicy {
  isSnapshotSchemaCompatible(version: string): boolean;
  isProjectorCodeCompatible(version: string): boolean;
}
