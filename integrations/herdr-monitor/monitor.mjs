import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';

const exec = promisify(execFile);
const SOURCE = 'openrig:monitor';
const identifier = /^[a-zA-Z0-9][a-zA-Z0-9_.:@-]*$/;
const seatName = /^[a-zA-Z0-9][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9_.-]*$/;
const fields = 'hostSelfId,rigName,canonicalSessionName,runtime,lifecycleState,agentActivity,arbitratedNeedsInput,pendingWorkCount,inProgressWorkCount,blockedWorkCount';
const requireThat = (ok, message) => { if (!ok) throw new Error(message); };
const safeId = (value) => typeof value === 'string' && identifier.test(value);
const sameArgv = (a, b) => Array.isArray(a) && a.length === b.length && a.every((v, i) => v === b[i]);

export function readBinding(filename) {
  requireThat(path.isAbsolute(filename), 'Use an absolute private binding path');
  const stat = fs.lstatSync(filename);
  requireThat(stat.isFile() && (stat.mode & 0o077) === 0, 'Binding must be a private regular file');
  const config = JSON.parse(fs.readFileSync(filename, 'utf8'));
  requireThat(config.version === 1 && safeId(config.session), 'Invalid binding version or Herdr session');
  for (const binary of [config.rig, config.herdr]) {
    requireThat(typeof binary === 'string' && path.isAbsolute(binary), 'Use absolute CLI paths');
    fs.accessSync(binary, fs.constants.X_OK);
  }
  const url = new URL(config.localUrl);
  requireThat(['http:', 'https:'].includes(url.protocol) && url.origin === config.localUrl, 'localUrl must be a daemon origin without credentials or path');
  config.intervalMs ??= 5000;
  config.maxAgeMs ??= 30000;
  requireThat(Number.isInteger(config.intervalMs) && config.intervalMs >= 500 && config.intervalMs <= 60000, 'intervalMs must be 500..60000');
  requireThat(Number.isInteger(config.maxAgeMs) && config.maxAgeMs >= config.intervalMs && config.maxAgeMs <= 600000, 'Invalid maxAgeMs');
  requireThat(Array.isArray(config.viewers) && config.viewers.length > 0, 'At least one viewer is required');
  const paneIds = new Set();
  for (const v of config.viewers) {
    requireThat([v.workspaceId, v.paneId, v.terminalId, v.hostId].every(safeId) && !paneIds.has(v.paneId), 'Invalid or repeated viewer identity');
    paneIds.add(v.paneId);
    requireThat(typeof v.label === 'string' && v.label.length > 0 && v.label.length <= 40 && !/[\x00-\x1f\x7f]/.test(v.label), 'Invalid display label');
    requireThat(seatName.test(v.seat) && (v.kind === 'hosted' ? safeId(v.roster) :
      v.kind === 'worker' && safeId(v.host) && safeId(v.rig) && v.rig === v.seat.split('@')[1]), 'Invalid seat observation target');
    requireThat(Array.isArray(v.foregroundArgv) && v.foregroundArgv.length > 0 && path.isAbsolute(v.foregroundArgv[0])
      && v.foregroundArgv.every((a) => typeof a === 'string' && a.length > 0 && !a.includes('\0')), 'Exact foreground argv required');
  }
  return config;
}

function unknown(reason, detail = {}) {
  return { state: 'unknown', reason, ...detail };
}

export function workerStatus(viewer, json, now, maxAgeMs) {
  const rows = Array.isArray(json) ? json : json?.entries;
  if (!Array.isArray(rows) || rows.length !== 1 || json.truncated || (json.totalNodes !== undefined && json.totalNodes !== 1)) return unknown('seat unavailable');
  const row = rows[0];
  if (row.hostSelfId !== viewer.hostId || row.rigName !== viewer.rig || row.canonicalSessionName !== viewer.seat) return unknown('seat identity changed');
  const counts = [row.pendingWorkCount, row.inProgressWorkCount, row.blockedWorkCount];
  const queue = counts.every((n) => Number.isInteger(n) && n >= 0) ? counts.join('/') : 'unknown';
  const info = { runtime: row.runtime ?? 'unknown', lifecycle: row.lifecycleState ?? 'unknown', queue };
  if (row.lifecycleState !== 'running') return unknown('seat not running', info);
  const activity = row.agentActivity;
  const timestamp = Date.parse(activity?.sampledAt);
  if (!Number.isFinite(timestamp) || activity?.stale === true || now - timestamp > maxAgeMs || timestamp > now + 2000) {
    return unknown('activity stale or unavailable', info);
  }
  const detail = { ...info, sampledAt: activity.sampledAt, basis: activity.evidenceSource ?? 'unknown' };
  if (row.arbitratedNeedsInput === true) return { state: 'needs input', reason: 'OpenRig input arbitration', ...detail };
  if (activity.state === 'needs_input' && row.arbitratedNeedsInput === false) return unknown('input request cleared', detail);
  const states = new Map([['idle', 'idle'], ['running', 'working'], ['needs_input', 'needs input']]);
  const state = states.get(activity.state);
  return state ? { state, reason: activity.reason ?? 'OpenRig observation', ...detail } : unknown('activity unknown', detail);
}

export function hostedStatus(viewer, json) {
  const members = json?.roster?.members;
  const matches = Array.isArray(members) ? members.filter((m) => m.seat === viewer.seat && m.host === viewer.hostId) : [];
  if (matches.length !== 1 || !matches[0].hosted || json.roster.id !== viewer.roster) return unknown('hosted seat unavailable');
  return unknown('hosted · activity unobserved', { hosted: true });
}

async function readStatus(config, viewer, client, now) {
  try {
    if (viewer.kind === 'hosted') return hostedStatus(viewer, await client.rig(['roster', 'show', viewer.roster, '--json']));
    const result = await client.rig(['ps', '--host', viewer.host, '--nodes', '--rig', viewer.rig, '--session', viewer.seat, '--json', '--fields', fields]);
    return workerStatus(viewer, result, now(), config.maxAgeMs);
  } catch {
    return unknown('OpenRig read unavailable');
  }
}

function paneMatches(pane, v) {
  return pane?.workspace_id === v.workspaceId && pane?.pane_id === v.paneId && pane?.terminal_id === v.terminalId;
}

async function verifyViewer(v, client) {
  const [pane, process] = await Promise.all([client.pane(v.paneId), client.process(v.paneId)]);
  requireThat(paneMatches(pane, v), 'viewer identity changed');
  requireThat(process?.pane_id === v.paneId && Array.isArray(process.foreground_processes)
    && process.foreground_processes.length === 1 && sameArgv(process.foreground_processes[0].argv, v.foregroundArgv), 'viewer foreground changed');
  return pane;
}

function textField(value) {
  return [...String(value).replace(/[\x00-\x1f\x7f]/g, '').trim()].slice(0, 80).join('');
}

function presentation(v, state, seq, expiresInMs) {
  const summary = state.hosted ? state.reason : state.state === 'unknown' ? 'unknown · ' + state.reason : state.state;
  const title = textField(v.label + ' · ' + summary);
  const tokens = {
    openrig_role: v.kind, openrig_seat: v.seat, openrig_host: v.hostId, openrig_state: state.state,
    openrig_reason: state.reason, openrig_sample: state.sampledAt ?? 'unobserved',
    openrig_queue: state.queue ?? 'unobserved', openrig_basis: state.basis ?? 'unobserved',
    openrig_update: String(seq)
  };
  const args = ['pane', 'report-metadata', v.paneId, '--source', SOURCE, '--title', title, '--seq', String(seq), '--ttl-ms', String(expiresInMs)];
  const labels = {};
  for (const nativeState of ['idle', 'working', 'blocked', 'done', 'unknown']) {
    labels[nativeState] = textField('OpenRig · ' + summary);
    args.push('--state-label', nativeState + '=' + labels[nativeState]);
  }
  for (const key of Object.keys(tokens)) {
    tokens[key] = textField(tokens[key]);
    args.push('--token', key + '=' + tokens[key]);
  }
  return { args, title, tokens, labels };
}

export function nativeClient(config, run = exec) {
  const timeout = Math.min(10000, Math.max(2500, 2 * config.intervalMs));
  const execute = async (file, args, env) => {
    const { stdout } = await run(file, args, { env, timeout, maxBuffer: 2 * 1024 * 1024, encoding: 'utf8' });
    return stdout;
  };
  const herdr = (args) => execute(config.herdr, ['--session', config.session, ...args], process.env);
  const lookup = async (args, key) => JSON.parse(await herdr(args)).result[key];
  return {
    rig: async (args) => JSON.parse(await execute(config.rig, args, { ...process.env, OPENRIG_URL: config.localUrl })),
    pane: (id) => lookup(['pane', 'get', id], 'pane'),
    process: (id) => lookup(['pane', 'process-info', '--pane', id], 'process_info'),
    publish: (args) => herdr(args)
  };
}

// Each cycle is serialized by watch. Timestamp sequences also order overlapping
// invocations by cycle start. Herdr ignores a replay; readback detects it.
export async function synchronize(config, { client = nativeClient(config), now = Date.now, dryRun = false } = {}) {
  const sequence = now();
  const results = await Promise.all(config.viewers.map(async (viewer) => {
    let state = await readStatus(config, viewer, client, now);
    const identity = () => ({ paneId: viewer.paneId, seat: viewer.seat, ...state });
    try {
      await verifyViewer(viewer, client);
      let ttl = config.intervalMs * 3;
      if (state.sampledAt) {
        const remaining = Date.parse(state.sampledAt) + config.maxAgeMs - now();
        if (remaining <= 0) state = { ...state, ...unknown('activity stale or unavailable') };
        else ttl = Math.min(ttl, remaining);
      }
      if (dryRun) return { ...identity(), verified: true, published: false };
      const overlay = presentation(viewer, state, sequence, ttl);
      await client.publish(overlay.args);
      const confirmed = await verifyViewer(viewer, client);
      requireThat(confirmed.title === overlay.title && Object.entries(overlay.tokens).every(([k, v]) => confirmed.tokens?.[k] === v)
        && Object.entries(overlay.labels).every(([k, v]) => confirmed.state_labels?.[k] === v),
        'Herdr did not confirm metadata');
      return { ...identity(), verified: true, published: true };
    } catch (error) {
      // Native stderr can contain screen/process data. Publish only bounded diagnostics.
      const reason = ['viewer identity changed', 'viewer foreground changed', 'Herdr did not confirm metadata'].includes(error.message)
        ? error.message : 'Herdr request unavailable';
      return { ...identity(), verified: false, published: false, error: reason };
    }
  }));
  return { ok: results.every((v) => v.verified && (dryRun || v.published)), dryRun, results };
}

export async function watch({ read, tick = synchronize, emit, sleep = delay, signal }) {
  let interval = 5000;
  while (!signal.aborted) {
    try {
      const config = read();
      interval = config.intervalMs;
      emit(await tick(config));
    } catch (error) {
      emit({ ok: false, error: error.message });
    }
    if (!signal.aborted) {
      try { await sleep(interval, undefined, { signal }); }
      catch (error) { if (!signal.aborted) throw error; }
    }
  }
}
