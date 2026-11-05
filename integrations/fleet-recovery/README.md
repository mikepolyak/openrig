# OpenRig fleet recovery guardian

`guardian.py --config /absolute/private/config.json` runs once. Existing launchd `RunAtLoad` plus `StartInterval=60` supplies supervision. No additional scheduler, provider polling process or shared-process cleanup is needed.

Config fields:

```json
{
  "runtime": "/absolute/external/runtime",
  "volume": "/Volumes/selected",
  "volumeUuid": "EXACT-DISKUTIL-VOLUME-UUID",
  "uid": 502,
  "hostId": "host-exact",
  "rig": "/absolute/external/runtime/bin/rig",
  "nodeBinary": "/absolute/node",
  "daemonScript": "/absolute/source/packages/daemon/dist/index.js",
  "allowInProgressRecovery": false,
  "adoptCaffeinatePid": 12345,
  "seats": [{
    "rigId": "EXACT-RIG-ID",
    "nodeId": "EXACT-NODE-ID",
    "logicalId": "verify.worker",
    "session": "verify-worker@project-heavy",
    "runtime": "codex"
  }]
}
```

`adoptCaffeinatePid` is optional and must refer to an already established task-owned sleep assertion with the exact current daemon PID. It is never killed or modified. New assertions use `caffeinate -i -w DAEMON_PID`; they end with that daemon. `run/guardian-caffeinate.json` records ownership and prevents duplicates across runs.

`allowInProgressRecovery` defaults off. When explicitly enabled, a dead seat may resume only after two consistent reads establish its exact original native token and queue claims. Every in-progress claim must have the same exact destination seat, a claim timestamp, and no blocker or human question; count, IDs and claim timestamps must agree on both reads. Blocked work and input waits remain held. The guardian never claims, reassigns, creates, retries or closes queue items. A recovered conversation re-reads its original task. Successful recovery receipts record the original active qitem IDs without task bodies or session tokens.

Codex can restore an interrupted conversation to an idle prompt without continuing its owned job. After proving that the exact original native occupant is running, the guardian re-reads ownership and issues one native `rig send SEAT ... --wait-for-idle 10 --json` continuation reminder for the original active qitems. It retains all native permission and interactive-prompt guards. The reminder asks the specialist to read the original rows and `knowledge/CONTINUITY.md`, preserve checkpoints and admitted scope, and return through the existing native handoff. It does not create or reclaim a task.

Before sending, an exclusive mode-0600 `run/guardian-wake-HASH.json` attempt receipt is flushed to disk. Its identity binds the seat, original conversation, current native process and exact original queue claims. No ambiguous send is retried, including after a supervisor process failure; the next normal pass preserves the now-live seat. Send acknowledgment is recorded separately from `taskConsumption: unverified`. Completion requires independent queue/result evidence. Guard refusal, retention or an ambiguous outcome leaves an exception receipt and unsuccessful supervisor status rather than falsely claiming job continuation. A later distinct executor crash can receive a new single wake for its new verified native process.

An unconfirmed wake remains degraded on later live-preserve passes for the same seat and native conversation, including after the executor PID changes. PID identifies send deduplication, not health reconciliation. Those passes read the original source qitem IDs without sending anything; legacy receipts without a conversation hash are checked conservatively against the exact node and source-row ownership. The exception clears only when all exact original source rows are natively terminal; the receipt records their terminal states and still says `reportAcceptance: unverified`. Native task closure or handoff never establishes acceptance of the report. Missing rows, changed destinations or unreadable queue evidence remain held. Explicit operational reconciliation can be performed separately under its own authority; the guardian does not invent it.

Recovery never calls `rig down`, `seat stop`, `seat clean`, `--fresh`, `--stop`, or a fresh launch. Only positive, recent `session_missing` or `pane_exited` verdicts qualify. A healthy live idle agent is preserved. Native single-seat launch plan must preserve all siblings; a separate id-grounded native `POST /api/rigs/:id/launch-plan` preview must forecast `resume-original`, verified token present, and no fresh requirement for the same snapshot. The read-only preview takes `{}`; unsupported snapshot request fields are never invented. Its returned snapshot ID must exactly match the single-seat selected snapshot, so a cadence race simply holds before applying. Null/malformed snapshot or node payloads also hold without throwing a traceback. Then the exact single-seat launch pins that snapshot. Whole-rig `rig start --last` is intentionally avoided: its unknown-seat fallback is not fail-closed and mixed intentional stops are selected at rig scope.

If the apply CLI times out, errors or emits malformed/non-clean output, it is **never retried**. The guardian polls native inventory for at most 20 seconds and accepts only the exact original seat, host, runtime, native resume token/type and verified running occupant. Successful reconciliation writes `launchCommandOutcome: ambiguous-reconciled`; anything unresolved holds without another apply. Even acknowledged commands require the same native readback.

Before an apply, an exclusive fsynced `run/guardian-launch-HASH.json` receipt binds the exact seat, native conversation, stable original occupant epoch and selected snapshot. The epoch is the persisted startup completion timestamp (or recorded native generation). Its attempt identity deliberately excludes snapshot selection: a new periodic snapshot cannot permit another apply to the same unresolved dead occupant. Later passes can only observe/reconcile the existing attempt. A genuinely new crash gets a new attempt only after the prior original executor was positively verified running and its replacement has a distinct persisted occupant epoch. Late successful applies are reconciled from the durable receipt and receive the same one-shot continuation policy when original owned work remains.

Before deploying, verify that the actual interpreter has ordinary macOS removable-volume permission using an owned one-shot launchd probe that reads the selected external runtime. Retain its receipt in private deployment evidence. Do not change the executable identity to evade a denial. Cold boot still needs ordinary FileVault login and the external volume mounted; this helper makes no pre-login guarantee.

Run focused verification with `python3 -m unittest -v test_guardian.py`. These are mocked policy/orchestration tests, not live recovery proof. Deployment acceptance requires a real same-conversation crash/restore and original queue-owner completion drill. Enable the in-progress recovery policy only after that proof.
