import { describe, expect, it } from 'vitest';
import {
  RunLifecycleState,
} from '../../../src/core/eventlog/event_types.js';
import { RunLifecycleManager } from '../../../src/operations/lifecycle/run_lifecycle.js';

describe('RunLifecycleManager', () => {
  it('allows dispatch only when the lifecycle is Ready', () => {
    const manager = new RunLifecycleManager();

    expect(manager.canDispatch(RunLifecycleState.Ready)).toBe(true);
    expect(manager.canDispatch(RunLifecycleState.WaitingResult)).toBe(false);
    expect(manager.canDispatch(RunLifecycleState.Completed)).toBe(false);
  });

  it('emits a lifecycle event for every authoritative transition', async () => {
    const manager = new RunLifecycleManager();
    const result = await manager.transition(
      RunLifecycleState.Ready,
      'action_admitted',
      'run-1',
    );

    expect(result.nextState).toBe(RunLifecycleState.WaitingResult);
    expect(result.events).toHaveLength(1);
    expect(result.events[0]?.eventType).toBe('run_lifecycle_transitioned');
  });

  it('treats Completed and Aborted as terminal states', async () => {
    const manager = new RunLifecycleManager();

    await expect(
      manager.transition(
        RunLifecycleState.Completed,
        'external_event_received',
        'run-1',
      ),
    ).rejects.toThrow(/terminal/i);

    await expect(
      manager.transition(RunLifecycleState.Aborted, 'action_admitted', 'run-1'),
    ).rejects.toThrow(/terminal/i);
  });
});
