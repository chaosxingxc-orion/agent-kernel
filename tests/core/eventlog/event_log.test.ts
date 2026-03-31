import { describe, expect, it } from 'vitest';
import { InMemoryEventLog } from '../../../src/core/eventlog/event_log.js';
import type { ActionCommit } from '../../../src/core/eventlog/event_types.js';

function makeCommit(): ActionCommit {
  return {
    commitId: 'commit-1',
    runId: 'run-1',
    actionId: 'action-1',
    events: [
      {
        eventId: 'evt-1',
        runId: 'run-1',
        actionId: 'action-1',
        eventType: 'external_effect_observed',
        eventClass: 'fact',
        eventAuthority: 'authoritative_fact',
        orderingKey: 'run-1',
        commitOffset: 0,
        eventTime: '2026-03-30T00:00:00Z',
        wakePolicy: 'wake_harness',
      },
      {
        eventId: 'evt-2',
        runId: 'run-1',
        actionId: 'action-1',
        eventType: 'verification_completed',
        eventClass: 'derived',
        eventAuthority: 'derived_replayable',
        orderingKey: 'run-1',
        commitOffset: 0,
        eventTime: '2026-03-30T00:00:00Z',
        wakePolicy: 'projection_only',
      },
    ],
  };
}

describe('InMemoryEventLog', () => {
  it('appends one action commit as one logical batch', async () => {
    const eventLog = new InMemoryEventLog();
    const ref = await eventLog.appendActionCommit(makeCommit());

    expect(ref.commitId).toBe('commit-1');
    expect(ref.runId).toBe('run-1');
    expect(ref.eventIds).toEqual(['evt-1', 'evt-2']);
    expect(ref.throughOffset).toBeGreaterThan(0);
  });
});
