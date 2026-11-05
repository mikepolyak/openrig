# Herdr monitor status

Show OpenRig's current seat or whole-rig observation on dedicated Herdr viewers. A desktop
control chat can be a hosted roster member with no observed process. A registered
worker can have a running process, an idle prompt and outstanding queue rows at
the same time. The integration keeps those facts separate.

## Bind a verified view

Open the native terminal view first. From its **returned workspace**, identify
each intended pane and terminal and inspect `herdr pane process-info --pane ID`.
Confirm the exact foreground command belongs to the intended observer. If tmux
uses a target such as `$2`, resolve it on that specific tmux socket and confirm
the expected session before binding. A pane title alone cannot establish this.

Save a private JSON binding (mode `0600`) outside the checkout. This example
uses placeholder identities and paths. Supply the actual values from each open.

```json
{
  "version": 1,
  "rig": "/private/openrig/bin/rig",
  "herdr": "/installed/bin/herdr",
  "session": "monitor",
  "localUrl": "http://127.0.0.1:7433",
  "intervalMs": 5000,
  "maxAgeMs": 30000,
  "viewers": [
    {
      "kind": "hosted", "label": "Control",
      "workspaceId": "w1", "paneId": "w1:p1", "terminalId": "term-control",
      "foregroundArgv": ["/installed/bin/tmux", "attach-session", "-t", "control-view"],
      "hostId": "host-origin", "seat": "desktop-control@team", "roster": "team"
    },
    {
      "kind": "worker", "label": "Remote auditor",
      "workspaceId": "w1", "paneId": "w1:p2", "terminalId": "term-worker",
      "foregroundArgv": ["/installed/bin/tmux", "attach-session", "-r", "-t", "worker-view"],
      "host": "worker-host", "hostId": "host-worker", "rig": "workers", "seat": "audit@workers"
    }
  ]
}
```

`rig` should be the wrapper for this installation's private OpenRig home.
`host` is the worker host's registered alias. `hostId` is its daemon's durable
self ID. Both are checked through the native worker read. For a hosted member,
use the host and roster containing the bound seat.

## Monitor every managed seat on a Mac

Use `kind: "fleet"` for a rig dashboard instead of treating one worker as the
status of the entire Mac. The pane/process identity fields stay the same. Supply
the expected managed seats explicitly so a missing worker remains visible as
unknown. All additional native seats in the rig are included automatically.

```json
{
  "kind": "fleet", "label": "Worker Mac",
  "workspaceId": "w1", "paneId": "w1:p2", "terminalId": "term-worker",
  "foregroundArgv": ["/installed/bin/tmux", "attach-session", "-r", "-t", "fleet-view"],
  "host": "worker-host", "hostId": "host-worker", "rig": "workers",
  "expectedSeats": ["audit@workers", "build@workers", "review@workers"]
}
```

Omit `host` to query the local daemon; remote reads use the registered alias.
The read checks every row against the expected daemon ID and rig, and refuses
truncated or duplicate identities. The title shows managed-seat totals and
working/idle/input/unknown counts. `openrig_queue` is the sum of native
pending/in-progress/blocked rows only when every seat has known counts.
`openrig_seat_1`, `openrig_seat_2`, etc. carry each canonical seat's activity and
queue counts. The compact `openrig_counts` token carries working/idle/unknown/stopped/input
counts. Herdr permits at most 16 tokens per metadata report, so pane metadata
previews up to six seats and `openrig_details` states the displayed/total count.
The native fleet dashboard and synchronization receipt retain the full roster.

A missing, stopped or stale seat prevents an all-idle aggregate. Fresh input
arbitration takes precedence, while the counts continue to expose unknown
members. The oldest native activity sample controls metadata expiry. Hosted
chat membership is separate from managed seats and remains **activity
unobserved**; use the existing hosted viewer when that identity needs its own
label. A fleet dashboard does not infer activity for the control chat.

After upgrading monitor source, restart only the recorded, exactly verified
watcher so it loads the new module; a binding-only update is read on the next
cycle without a restart.

## Run

Use Node 22 from the checkout. Dry run verifies the binding and reports status.
One-shot mode publishes once. Watch mode refreshes until SIGINT or SIGTERM.

```sh
node integrations/herdr-monitor/cli.mjs --binding /private/control/monitor.json --dry-run once
node integrations/herdr-monitor/cli.mjs --binding /private/control/monitor.json once
node integrations/herdr-monitor/cli.mjs --binding /private/control/monitor.json watch
```

After a new open, atomically replace the binding with the new confirmed
workspace, pane, terminal and foreground identities. The watcher reads it every
cycle. It addresses the named session and explicit pane IDs. A changed mapping
refuses publication. A one-shot failure exits nonzero. The watcher emits one
JSON result per cycle and retries after a failed read.

## Read status

- A matched hosted member displays **hosted · activity unobserved**. Membership
  alone supplies no process or turn activity.
- For a managed worker, the native `rig ps --host` read must return one row
  matching host, rig and canonical seat. A fresh `running` observation displays
  **working**, `idle` displays **idle**, and an observed input request displays
  **needs input**. The native arbitrated input flag takes precedence. A cleared
  signal contradicting a pending request leaves activity unknown.
- Disconnection, changed host identity, missing seat, nonrunning lifecycle,
  stale or unavailable activity displays **unknown** with the reason. Separate
  tokens carry the evidence source, sample time and pending/in-progress/blocked
  row counts. Counts never establish activity. The viewer's foreground proves
  only the observed attachment. Worker state comes from the daemon that owns it.
- The publisher uses only `pane report-metadata`. It sets the title, presentation
  labels and `openrig_*` tokens, then confirms them with a pane read. Titles and
  labels show the OpenRig observation where the Herdr layout renders those
  fields. Herdr's native `agent_status`, waits and notification rollups continue
  to reflect Herdr's own detector. It can read `unknown` for a nested tmux viewer.
  [Herdr metadata API](https://herdr.dev/tui/docs/socket-api/)
- Metadata expires after three polling intervals at most. A live activity label
  expires sooner when its underlying sample reaches `maxAgeMs`. On watcher or
  transport failure, the last metadata disappears at expiry. An individual
  cycle may return `ok: true` while showing an observed **unknown** state. That
  receipt establishes successful publication of the uncertainty.

The native client runs argument arrays without a shell. It does not open panes,
claim Herdr agent lifecycle authority, send input, resume workers or change
OpenRig state. The exported `synchronize(config, {client, now, dryRun})` and
`watch({read, tick, emit, sleep, signal})` functions accept injected boundaries
for focused tests.

```sh
node --test integrations/herdr-monitor/monitor.test.mjs
```

## Automatic monitoring launcher

Run `node integrations/herdr-monitor/open.mjs /absolute/private/launch.json` to open the saved OpenRig view, verify its exact pane and tmux session identities, bind the observation targets and keep one status watcher running. Reopening uses OpenRig's normal existing-view verification. The mapping is refreshed when the verified view changes.

The private launcher configuration contains `version: 1`, absolute `rig`, `herdr`, `tmux`, `tmuxSocket` and `bindingPath` paths, `view`, `session`, `localUrl`, and observation `viewers`. Each viewer declares its `matchLabel`, `tmuxSession` and optional `readOnly` along with the observation fields documented above. Keep this file mode `0600` outside the source checkout. The launcher derives workspace, pane, terminal and foreground identities from the actual OpenRig return and Herdr process inventory. It verifies the socket-specific tmux session name and ID before replacing the private observation binding.

The launcher uses the daemon's structured `recoveryArgv` preview and returned workspace identity. Explicit tmux socket routing is required when the daemon uses an external `TMUX_TMPDIR` and the Herdr server has a different environment. Native lifecycle badges remain Herdr's observations of the attached viewer. Expiring OpenRig display labels show the remote worker activity and identify a hosted control seat as unobserved.
