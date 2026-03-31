/** Enumerates the authoritative lifecycle states for one run. */
export enum RunLifecycleState {
  Created = 'Created',
  Ready = 'Ready',
  Dispatching = 'Dispatching',
  WaitingResult = 'WaitingResult',
  WaitingExternal = 'WaitingExternal',
  Recovering = 'Recovering',
  Completed = 'Completed',
  Aborted = 'Aborted',
}

/** Represents one append-only domain event in the runtime event log. */
export interface RuntimeEvent {
  eventId: string;
  runId: string;
  actionId?: string;
  eventType: string;
  eventClass: 'fact' | 'derived';
  eventAuthority:
    | 'authoritative_fact'
    | 'derived_replayable'
    | 'derived_diagnostic';
  orderingKey: string;
  idempotencyKey?: string;
  commitOffset: number;
  eventTime: string;
  wakePolicy: 'wake_harness' | 'projection_only';
  payload?: Record<string, unknown>;
}

/** Represents one action-level commit that batches multiple events atomically. */
export interface ActionCommit {
  commitId: string;
  runId: string;
  actionId: string;
  events: RuntimeEvent[];
}

/** Represents the append result reference for one committed action batch. */
export interface CommitBatchRef {
  commitId: string;
  runId: string;
  throughOffset: number;
  eventIds: string[];
}

/** Represents one same-run mailbox batch coalesced from multiple commit refs. */
export interface RunMailboxBatch {
  runId: string;
  refs: CommitBatchRef[];
  maxThroughOffset: number;
  wakeReasons: string[];
}

/** Represents one dispatchable action envelope produced by projection. */
export interface ActionEnvelope {
  runId: string;
  actionId: string;
  actionType: string;
  payload: Record<string, unknown>;
}

/** Represents the minimal dispatch manifest needed for admission checks. */
export interface DispatchManifest {
  actionType: string;
}

/** Represents the resolved dispatch artifact consumed by admission. */
export interface DispatchArtifact {
  artifactKey: string;
  manifest: DispatchManifest;
  resolvedHandler: string;
  permissionTemplate: string[];
  staticDependencyGraph: string[];
  compileVersion: string;
}

/** Represents the admission decision returned by the policy gate. */
export interface DispatchDecision {
  admitted: boolean;
  reasonCode: string;
}

/** Represents executor output events generated for one action execution. */
export interface ExecutionResultEnvelope {
  factEvents: RuntimeEvent[];
  derivedEvents: RuntimeEvent[];
  failureCode?: string;
}

/** Represents the authoritative decision projection for one run. */
export interface RunProjection {
  runId: string;
  lifecycleState: RunLifecycleState;
  currentActionId?: string;
  waitingExternal: boolean;
  readyForDispatch: boolean;
  recoveryMode?: string;
  projectedOffset: number;
  nextAction?: ActionEnvelope;
  dispatchArtifact?: DispatchArtifact;
}

/** Represents projection readiness for a required runtime offset. */
export interface DecisionReadiness {
  ready: boolean;
  requiredOffset: number;
}

/** Represents the actor-level decision for the next runtime step. */
export interface HarnessDecision {
  action: 'enqueue' | 'wait' | 'recover' | 'complete' | 'noop';
  actionRef?: string;
  reasonCode: string;
}

/** Represents the recovery gate input envelope for one execution failure. */
export interface RecoveryInput {
  runId: string;
  actionId?: string;
  basedOnOffset: number;
  failureCode: string;
  error: Error;
}

/** Represents the recovery gate output decision and related events. */
export interface RecoveryDecision {
  nextState: RunLifecycleState;
  events: RuntimeEvent[];
}

/** Abstracts append-only event logging as kernel authority. */
export interface EventLog {
  appendActionCommit(commit: ActionCommit): Promise<CommitBatchRef>;
  load(runId: string, afterOffset?: number): Promise<RuntimeEvent[]>;
}

/** Abstracts catch-up and readiness checks over the decision projection. */
export interface DecisionProjection {
  catchUp(runId: string, throughOffset: number): Promise<RunProjection>;
  readiness(runId: string, requiredOffset: number): Promise<DecisionReadiness>;
  get(runId: string): Promise<RunProjection>;
}

/** Abstracts dispatch-time admission policy checks. */
export interface Admission {
  admit(
    envelope: ActionEnvelope,
    artifact: DispatchArtifact,
  ): Promise<DispatchDecision>;
}

/** Abstracts execution of one admitted action. */
export interface Executor {
  execute(envelope: ActionEnvelope): Promise<ExecutionResultEnvelope>;
}

/** Abstracts recovery mode selection for one failed action. */
export interface Recovery {
  decide(input: RecoveryInput): Promise<RecoveryDecision>;
}
