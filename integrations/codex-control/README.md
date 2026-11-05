# Codex control adapter

A small local adapter between a persistent operator chat and OpenRig's native
queue CLI. It does not start daemons or agents, create a chat, schedule a wake,
copy report files, or read SQLite. Native queue state remains authoritative.

The user-facing chat may be closed while managed workers continue. A separately
configured app heartbeat can use this adapter to reconcile work and deliver new
results. Pinning a chat alone does not provide background execution or wakeup.

## Private binding

Keep the binding and receipts outside this public source repository. The binding
must be a regular file with mode `0600` or stricter. `stateDir` is created with
mode `0700`; an existing directory with group/other access is refused. The adapter
never changes existing permissions. No credential values belong in this file.
The native CLI can use its configured host registry under `local.home`.

```json
{
  "version": 1,
  "thread": { "id": "your-thread-id", "host": "local" },
  "cli": {
    "node": "/absolute/path/to/node",
    "path": "/absolute/path/to/openrig/packages/cli/dist/bin-wrapper.js"
  },
  "local": {
    "home": "/private/runtime/openrig",
    "url": "http://127.0.0.1:7433",
    "session": "desktop-control@team"
  },
  "stateDir": "/private/control/receipts",
  "targets": {
    "worker": {
      "host": "worker-host",
      "url": "http://127.0.0.1:17433",
      "destination": "audit-user@workers"
    }
  },
  "timeoutMs": 30000
}
```

`host` is the registered remote HTTP host alias, used only on remote `queue create`.
Omit it for a destination on the local daemon. `url` is the receiving daemon's
read endpoint, normally an SSH-forwarded loopback port. Native queue reads do not
accept `--host`. The two values must designate the same daemon; verify with
`rig host doctor` during deployment. Binding URLs cannot contain credentials.

`thread` is a durable correlation identity, not proof of desktop authentication.
The CLI declares `OPENRIG_SESSION_NAME` locally; this adapter is for the existing
trusted-machine boundary, not a public or multi-user API.

## Commands

Run from the source checkout, with an absolute binding path:

```sh
node integrations/codex-control/cli.mjs --binding /private/control/binding.json submit --id audit-001 --target worker --body-file /private/control/brief.md --summary 'Audit the requested user flow'
node integrations/codex-control/cli.mjs --binding /private/control/binding.json status --id audit-001
node integrations/codex-control/cli.mjs --binding /private/control/binding.json transitions --id audit-001
node integrations/codex-control/cli.mjs --binding /private/control/binding.json inbox --limit 100
node integrations/codex-control/cli.mjs --binding /private/control/binding.json accept --id returned-result-id --resolution-file /private/control/acceptance.md --evidence-ref reports/audit-001/report.md
```

- **submit** atomically persists ID, source/thread/target identity, summary and body
  SHA-256 before invoking OpenRig. It keeps a private exact body snapshot. Identical
  retries reuse the same native ID. Conflicting reuse is refused before submission.
  A failed/timeout submit triggers one read of that ID on the receiving daemon.
  Only a uniquely identified row with matching destination and body hash is
  reconciled as existing; other outcomes remain `indeterminate`. Retry only the
  identical ID/body, or inspect the returned native diagnostics. Queue persistence
  and wake receipts are not proof of pickup or completion.
- **status/transitions** consult the receiving daemon for the recorded job. A tunnel
  URL can change; thread/source/host/destination identity cannot silently change.
- **inbox** lists local rows for the bound control seat, including closed history.
  The list is bounded by `--limit`; its response does not claim exhaustive coverage.
- **accept** reads a returned local result, verifies its destination, claims it if
  pending, then closes it with the authored resolution and optional evidence ref.
  It refuses unknown/blocked/ambiguous rows. A terminal row is returned unchanged.
  The caller must review the actual evidence before invoking acceptance. A close
  timeout stays indeterminate; inspect the inbox before retrying.

All subprocesses use argument arrays, without a shell. Ambient `OPENRIG_*`
variables are removed and replaced with the binding's explicit home, URL and
session. This avoids an inherited worker identity or selected-host environment
silently redirecting a desktop operation. Other environment variables are retained.

Each native response and reconciliation is returned raw inside `native` or
`reconciliation` and written to a private atomic receipt. The adapter does not
interpret successful submission as completion, accept findings automatically, or
record an app message as delivered. The app-side consumer must reconcile its own
message delivery using task/result identities and report hashes.

## Return a report across hosts

After a worker has claimed the source job and written its report, it must return
the work with one native `queue handoff`. Run it in the worker seat against the
worker's daemon. `ORIGIN_ALIAS` is that daemon's registered alias for the
control host. Use a short result summary as the body and a stable reference to
the preserved report as evidence.

```sh
rig queue handoff "$SOURCE_JOB_ID" --to "$CONTROL_SEAT" --host "$ORIGIN_ALIAS" \
  --body-file "$REPORT_SUMMARY_FILE" --summary "$RESULT_SUBJECT" \
  --evidence-ref "$REPORT_REF" --full --json
```

For a hosted control seat deliberately polled by an app heartbeat, add `--no-nudge` to this result handoff. This records intentional cold-queue delivery instead of a failed terminal wake. Keep normal nudges for managed worker destinations; do not disable fleet recovery or register a dummy wake receiver.

Save the receipt. OpenRig derives `created.qitemId` from the source job ID,
destination seat and exact host alias. It first creates the successor on the
control host, appending the source job ID to its `chainOfRecord`. It then
marks the source `handed-off` with a `closureTarget` of
`<created.qitemId>@<ORIGIN_ALIAS>`. Creating a separate result row and manually
closing the source against a bare ID omits this link and can trigger false
custody recovery.

A later custody check can still ask for verification on the named host. Read
that successor ID on the control daemon and retain the receipt. If the receiver
is unavailable, leave custody unverified until it can answer.

If the response is lost, read the source on the worker daemon and the bound
seat's inbox on the control daemon. For a partial handoff, retry the same
command with the same source, destination, exact host alias, body and evidence.
The native retry reuses the successor ID. A conflicting existing close must be
investigated before taking further action.

On the control host, read `rig queue show "$RESULT_ID" --full --json` using the
actual `created.qitemId`. Confirm its destination and source job in
`chainOfRecord`. Retrieve and verify the referenced report, then run the
adapter's `accept --id "$RESULT_ID"` with the reviewer's resolution. A handoff
records custody of the queue row. Report transfer, acceptance and delivery to
the chat each need their own evidence.

## Deployment and verification

1. Build the pinned OpenRig source with its documented `npm ci` and `npm run build`.
2. Verify both daemons and HTTP routes, the destination worker seat, and the return
   route. The desktop seat can be a hosted roster member without a tmux pane.
3. Write the private binding and set mode `0600`. Verify its daemon routes against
   `rig host doctor`; do not point reads at a different instance.
4. Submit one bounded job; verify the worker's own claim transition and evidence.
5. Have the worker hand the result to the bound desktop seat. Review and accept it.
6. Configure the supported desktop heartbeat/wake path separately. Test an actual
   completion delivered after the chat becomes idle, and a reconnect case. Stay
   quiet on unchanged state; do not treat an accepted wake as a consumed result.

Focused adapter tests use fake subprocess responses and temporary private state:

```sh
node --test integrations/codex-control/adapter.test.mjs
node --check integrations/codex-control/adapter.mjs
node --check integrations/codex-control/cli.mjs
```

These tests prove adapter arguments, identity, persistence, retry/reconciliation
and recipient checks. They do not prove native daemon behavior, provider execution,
SSH health, browser audit quality or Codex desktop wake/report delivery.
