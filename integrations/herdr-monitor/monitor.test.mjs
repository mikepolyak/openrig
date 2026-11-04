import test from 'node:test';
import assert from 'node:assert/strict';
import { synchronize, workerStatus, hostedStatus, nativeClient, watch } from './monitor.mjs';

const now = Date.parse('2026-01-01T12:00:00Z');
const time = () => now;
const v = { kind: 'worker', label: 'Remote', workspaceId: 'w1', paneId: 'w1:p2', terminalId: 'term-2',
  foregroundArgv: ['/bin/tmux', 'attach-session', '-r', '-t', 'observer'], hostId: 'host-worker', seat: 'audit@work', rig: 'work', host: 'peer' };
const h = { ...v, kind: 'hosted', label: 'Origin', paneId: 'w1:p1', terminalId: 'term-1', hostId: 'host-local', seat: 'control@team', roster: 'group' };
const b = { intervalMs: 5000, maxAgeMs: 30000, viewers: [v] };
const activity = { state: 'idle', reason: 'idle_prompt', sampledAt: new Date(now).toISOString(), evidenceSource: 'pane_heuristic' };
const row = { hostSelfId: v.hostId, canonicalSessionName: v.seat, rigName: v.rig, runtime: 'codex', lifecycleState: 'running', agentActivity: activity,
  pendingWorkCount: 0, inProgressWorkCount: 1, blockedWorkCount: 2 };
const hosted = { roster: { id: h.roster, members: [{ seat: h.seat, host: h.hostId, hosted: true, live: { agentActivity: { state: 'idle' } } }] } };
const result = (data) => workerStatus(v, data, now, b.maxAgeMs);
const flag = (args, key) => args[args.indexOf(key) + 1];

function clientFor(viewer, data) {
  const pane = { workspace_id: viewer.workspaceId, pane_id: viewer.paneId, terminal_id: viewer.terminalId, agent_status: 'unknown', tokens: {}, state_labels: {} };
  const calls = [];
  return { calls, stored: pane,
    rig: async (args) => { calls.push(['rig', args]); return data; },
    pane: async () => pane,
    process: async () => ({ pane_id: viewer.paneId, foreground_processes: [{ argv: viewer.foregroundArgv }] }),
    publish: async (args) => {
      calls.push(['publish', args]); pane.title = flag(args, '--title');
      for (let i = 0; i < args.length; i++) {
        if (args[i] === '--token' || args[i] === '--state-label') {
          const [k, value] = args[i + 1].split('='); (args[i] === '--token' ? pane.tokens : pane.state_labels)[k] = value;
        }
      }
    }
  };
}

test('native activity, lifecycle and queue counts stay distinct', () => {
  assert.equal(result([row]).state, 'idle'); assert.equal(result([row]).queue, '0/1/2');
  for (const [state, desired] of [['running', 'working'], ['needs_input', 'needs input'], ['unknown', 'unknown']]) {
    assert.equal(result([{ ...row, agentActivity: { ...activity, state } }]).state, desired);
  }
  assert.equal(result([{ ...row, arbitratedNeedsInput: true }]).state, 'needs input');
  assert.equal(result([{ ...row, arbitratedNeedsInput: false, agentActivity: { ...activity, state: 'needs_input' } }]).state, 'unknown');
  assert.equal(result([{ ...row, lifecycleState: 'stopped' }]).state, 'unknown');
});

test('ambiguous, retargeted and stale observations cannot report active work', () => {
  for (const json of [[], [row, row], { entries: [row], totalNodes: 2 }, [{ ...row, hostSelfId: 'foreign' }], [{ ...row, rigName: 'elsewhere' }],
    [{ ...row, canonicalSessionName: 'someone@work' }], [{ ...row, agentActivity: { ...activity, stale: true } }],
    [{ ...row, agentActivity: { ...activity, sampledAt: 'bad' } }], [{ ...row, agentActivity: { ...activity, sampledAt: new Date(now - 30001).toISOString() } }],
    [{ ...row, agentActivity: { ...activity, sampledAt: new Date(now + 60000).toISOString() } }]]) {
    assert.equal(result(json).state, 'unknown');
  }
});

test('hosted roster membership proves identity but supplies no native agent activity', () => {
  assert.equal(hostedStatus(h, hosted).state, 'unknown'); assert.equal(hostedStatus(h, hosted).hosted, true);
  assert.equal(hostedStatus(h, { roster: { id: 'other', members: hosted.roster.members } }).hosted, undefined);
  assert.equal(hostedStatus(h, { roster: { id: 'group', members: [{ ...hosted.roster.members[0], hosted: false }] } }).hosted, undefined);
});

test('verified viewer receives only expiring metadata and labels; lifecycle remains native', async () => {
  const c = clientFor(v, { entries: [row], totalNodes: 1, truncated: false });
  const out = await synchronize(b, { client: c, now: time });
  assert.equal(out.ok, true); assert.equal(out.results[0].published, true);
  assert.equal(c.stored.title, 'Remote · idle'); assert.equal(c.stored.agent_status, 'unknown'); assert.equal(c.stored.state_labels.unknown, 'OpenRig · idle');
  const [verb, argv] = c.calls[1]; assert.equal(verb, 'publish'); assert.deepEqual(argv.slice(0, 3), ['pane', 'report-metadata', v.paneId]);
  assert.equal(flag(argv, '--ttl-ms'), '15000'); assert.equal(flag(argv, '--seq'), String(now));
  assert.equal(c.stored.tokens.openrig_queue, '0/1/2');
  assert.deepEqual(c.calls[0][1].slice(0, 8), ['ps', '--host', 'peer', '--nodes', '--rig', 'work', '--session', 'audit@work']);
});

test('a hosted dashboard gets a hosted label and no manufactured provider badge', async () => {
  const c = clientFor(h, hosted); const out = await synchronize({ ...b, viewers: [h] }, { client: c, now: time });
  assert.equal(out.ok, true); assert.equal(c.stored.tokens.openrig_state, 'unknown'); assert.match(c.stored.title, /hosted · activity unobserved/);
  assert.deepEqual(c.calls[0][1], ['roster', 'show', h.roster, '--json']);
});

test('an unreachable worker displays unknown, while slow native evidence expires by its own age', async () => {
  const c = clientFor(v, [row]); c.rig = async () => { throw new Error('tunnel unavailable'); };
  const out = await synchronize(b, { client: c, now: time }); assert.equal(out.ok, true); assert.equal(c.stored.tokens.openrig_state, 'unknown');
  assert.match(c.stored.title, /read unavailable/);
  const near = clientFor(v, [{ ...row, agentActivity: { ...activity, sampledAt: new Date(now - 29000).toISOString() } }]);
  await synchronize(b, { client: near, now: time }); assert.equal(flag(near.calls[1][1], '--ttl-ms'), '1000');
});

test('changed pane, terminal, workspace or foreground argv refuses every metadata write', async () => {
  for (const fault of ['pane_id', 'terminal_id', 'workspace_id', 'argv', 'multi']) {
    const c = clientFor(v, [row]);
    if (fault === 'argv' || fault === 'multi') {
      c.process = async () => ({ pane_id: v.paneId, foreground_processes: fault === 'argv' ? [{ argv: ['/bin/sh'] }] : [{ argv: v.foregroundArgv }, { argv: v.foregroundArgv }] });
    } else c.stored[fault] = 'foreign';
    const out = await synchronize(b, { client: c, now: time });
    assert.equal(out.ok, false); assert.equal(c.calls.some(([verb]) => verb === 'publish'), false);
  }
});

test('ignored or misdirected metadata is never confirmed; dry run has no writes', async () => {
  const c = clientFor(v, [row]); c.publish = async () => {};
  const out = await synchronize(b, { client: c, now: time }); assert.equal(out.ok, false); assert.match(out.results[0].error, /did not confirm/);
  const dry = await synchronize(b, { client: c, now: time, dryRun: true }); assert.equal(dry.ok, true); assert.equal(dry.results[0].published, false);
});

test('native calls pin the Herdr session and local OpenRig endpoint, never a focused pane', async () => {
  const seen = []; const c = nativeClient({ ...b, session: 'monitor', herdr: '/apps/herdr', rig: '/apps/rig', localUrl: 'http://127.0.0.1:7433' },
    async (exe, args, options) => { seen.push({ exe, args, options }); return { stdout: '{"result":{"pane":{"pane_id":"w1:p2"}}}' }; });
  assert.equal((await c.pane(v.paneId)).pane_id, v.paneId); await c.rig(['ps']);
  assert.deepEqual(seen[0].args, ['--session', 'monitor', 'pane', 'get', v.paneId]);
  assert.equal(seen[1].options.env.OPENRIG_URL, 'http://127.0.0.1:7433');
});

test('watch rereads replaced bindings, recovers from bad reads, and exits without a late cycle', async () => {
  const ac = new AbortController(); let reads = 0; const seen = [];
  await watch({ signal: ac.signal, read: () => { if (++reads === 1) throw new Error('bad mapping'); return { intervalMs: 1000, revision: reads }; },
    tick: async (config) => ({ ok: true, revision: config.revision }), emit: (v) => { seen.push(v); if (seen.length === 3) ac.abort(); },
    sleep: async () => {} });
  assert.deepEqual(seen, [{ ok: false, error: 'bad mapping' }, { ok: true, revision: 2 }, { ok: true, revision: 3 }]);
});
