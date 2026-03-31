import type { RuntimeEvent } from '../../core/eventlog/event_types.js';
import { RunLifecycleState } from '../../core/eventlog/event_types.js';

/** Carries lifecycle transition output plus emitted authoritative events. */
export interface LifecycleTransitionResult {
  nextState: RunLifecycleState;
  events: RuntimeEvent[];
}

/** Owns lifecycle transition rules for one run state machine. */
export class RunLifecycleManager {
  private transitionSequence = 0;

  /** Applies one trigger and returns the next authoritative lifecycle state. */
  public async transition(
    current: RunLifecycleState,
    trigger: string,
    runId: string,
  ): Promise<LifecycleTransitionResult> {
    if (this.isTerminal(current)) {
      throw new Error('terminal lifecycle state cannot transition');
    }
    const nextState = this.resolveNextState(current, trigger);
    this.transitionSequence += 1;
    return {
      nextState,
      events: [
        {
          eventId: `evt-lifecycle-${runId}-${this.transitionSequence}`,
          runId,
          eventType: 'run_lifecycle_transitioned',
          eventClass: 'fact',
          eventAuthority: 'authoritative_fact',
          orderingKey: runId,
          commitOffset: 0,
          eventTime: new Date().toISOString(),
          wakePolicy: 'wake_harness',
          payload: {
            trigger,
            previousState: current,
            nextState,
          },
        },
      ],
    };
  }

  /** Returns whether dispatch is currently allowed for a lifecycle state. */
  public canDispatch(state: RunLifecycleState): boolean {
    return state === RunLifecycleState.Ready;
  }

  /** Returns true when the lifecycle no longer accepts authoritative transitions. */
  private isTerminal(state: RunLifecycleState): boolean {
    return state === RunLifecycleState.Completed || state === RunLifecycleState.Aborted;
  }

  /** Resolves one transition trigger using minimal v6.4 lifecycle rules. */
  private resolveNextState(
    current: RunLifecycleState,
    trigger: string,
  ): RunLifecycleState {
    if (trigger === 'action_admitted') {
      return RunLifecycleState.WaitingResult;
    }
    if (trigger === 'action_dispatched') {
      return RunLifecycleState.Dispatching;
    }
    if (trigger === 'external_event_received') {
      return RunLifecycleState.Ready;
    }
    return current;
  }
}
