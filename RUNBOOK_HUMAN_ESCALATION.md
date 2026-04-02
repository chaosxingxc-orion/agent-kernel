# Runbook: Human Escalation Recovery Path

> Applies to: agent-kernel v6.4+  
> Recovery mode: `human_escalation`  
> Audience: on-call engineers, platform operators

---

## 1. What is human_escalation?

`human_escalation` is one of three kernel-native Recovery modes.  It is
triggered when the Recovery authority determines that a failed action cannot be
safely compensated automatically and requires human judgment.

**It is NOT a crash or data-loss event.**  The kernel has already:
1. Persisted the `FailureEnvelope` to `RecoveryOutcomeStore` (immutable record).
2. Emitted `recovery.human_escalation` to `RuntimeEventLog` (append-only truth).
3. Transitioned the run to `waiting_external` state (parked, no further dispatch
   until a human resumes it).

The run will remain in `waiting_external` until an operator sends a
`resume_from_snapshot` or `cancel_requested` signal.

---

## 2. Trigger conditions

| Condition | Why escalation fires |
|-----------|----------------------|
| `effect_class == "irreversible_write"` and execution result is `effect_unknown` | Effect cannot be verified or rolled back automatically |
| Recovery planner classifies failure reason as `human_*` prefix | Explicit operator-intent category |
| All compensation actions have been exhausted (max retries) | Kernel cannot safely continue without human decision |
| `waiting_human_input` heartbeat timeout | Human did not respond within the 24-hour policy window |

---

## 3. Detection

### 3.1 Logs

Look for the following structured log line from the Recovery authority:

```
recovery.human_escalation run_id=<id> failure_reason=<reason>
  evidence_ref=<ref> action_id=<id>
```

### 3.2 Event stream

Query the run's event stream for `recovery.human_escalation` events:

```python
async for event in facade.stream_run_events(run_id):
    if event.event_type == "recovery.human_escalation":
        print(event.payload_json)  # contains failure_reason, evidence_ref
```

### 3.3 Projection query

```python
response = await facade.query_run(QueryRunRequest(run_id=run_id))
# response.projection.lifecycle_state == "waiting_external"
# response.projection.recovery_reason == "human_escalation"
```

---

## 4. Triage decision tree

```
Run in waiting_external + recovery_reason=human_escalation
              │
              ▼
    Retrieve FailureEnvelope
    (payload_json in recovery.human_escalation event)
              │
    ┌─────────┴─────────────────────────────┐
    │                                       │
    ▼                                       ▼
effect verified externally?           effect UNKNOWN / BAD
(idempotent, safe to retry)           (irreversible or data concern)
    │                                       │
    ▼                                       ▼
Resume: send                         Abort: send
resume_from_snapshot signal          cancel_requested signal
(static_compensation retry)          (run terminates cleanly)
```

---

## 5. Resolution steps

### 5.1 Resume the run (safe to retry)

When the external system confirms the effect was not applied, or the action is
idempotent and safe to dispatch again:

```python
from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import SignalRunRequest

await facade._workflow_gateway.signal_workflow(
    run_id,
    SignalRunRequest(
        run_id=run_id,
        signal_type="resume_from_snapshot",
        signal_payload={"operator_note": "manually verified — safe to retry"},
        caused_by="operator:<your-name>",
    ),
)
```

The run will re-enter `recovering` → `static_compensation` → retry path.

### 5.2 Abort the run

When the effect is confirmed applied or the run must not continue:

```python
await facade._workflow_gateway.signal_workflow(
    run_id,
    SignalRunRequest(
        run_id=run_id,
        signal_type="cancel_requested",
        signal_payload={"operator_note": "effect confirmed applied; aborting"},
        caused_by="operator:<your-name>",
    ),
)
```

The run will transition to `aborted` (terminal).  No further dispatch occurs.

---

## 6. Post-escalation audit

After every `human_escalation` event, record the following in your incident
tracker:

| Field | Source |
|-------|--------|
| `run_id` | Event payload |
| `action_id` | Event payload / FailureEnvelope |
| `failure_reason` | FailureEnvelope.failure_reason |
| `evidence_ref` | FailureEnvelope.evidence_ref |
| `resolution` | resumed / aborted |
| `operator` | Your name + timestamp |
| `root_cause` | Investigation note |

Persistent `RecoveryOutcome` records can be retrieved from `RecoveryOutcomeStore`
for post-mortem analysis:

```python
outcome = await recovery_outcome_store.latest_for_run(run_id)
print(outcome.outcome, outcome.failure_reason, outcome.recorded_at)
```

---

## 7. SLA guidance

| State | Recommended action window |
|-------|--------------------------|
| `waiting_human_input` | ≤ 24 h (kernel heartbeat fires at 24 h) |
| `waiting_external` (escalation) | ≤ 4 h for business-critical runs |
| `waiting_external` (escalation) | ≤ 24 h for non-critical background runs |

If no action is taken within the heartbeat policy window, the kernel watchdog
will inject a `heartbeat_timeout` signal.  This routes through Recovery again
and may produce another escalation or an `abort`, depending on the planner
heuristic for the updated failure evidence.

---

## 8. Escalation routing configuration

The `human_escalation` recovery mode routes to a `recovery_channel` defined in
the `RecoveryDecision` payload.  Platform teams MUST wire a notification handler
(e.g. PagerDuty, Slack webhook) for this channel in their platform adapter layer.

The kernel emits the channel reference in the `recovery.human_escalation` event
`payload_json.recovery_channel` field.  Routing to the correct on-call channel
is the **platform layer's responsibility**; the kernel guarantees delivery to the
event log and the `RecoveryOutcomeStore`.

---

## 9. Common failure reasons and recommended resolutions

| `failure_reason` prefix | Likely cause | Resolution |
|--------------------------|--------------|------------|
| `human_approval_denied` | Approval gate rejected the action | Abort run; notify requester |
| `human_timeout` | Approval not received in time | Operator decides: resume or abort |
| `effect_unknown` | Executor returned ambiguous result | Verify externally → resume or abort |
| `irreversible_write_uncertain` | Write may have partially applied | Verify externally; do not retry blindly |
| `external_ack_missing` | External system did not confirm receipt | Check external system health; resume if safe |

---

## 10. Architecture reference

The `human_escalation` path preserves the six-authority invariant:

```
FailureEnvelope captured by Executor
    → RecoveryGateService.decide() → human_escalation mode
    → RecoveryOutcomeStore.write_outcome() (immutable)
    → RuntimeEventLog.append(recovery.human_escalation) (append-only)
    → run.lifecycle_state = waiting_external
    → STOP (no further dispatch until operator signal)
```

Recovery NEVER mutates the event log retroactively.  The audit trail is
permanent and available for replay at any time.
