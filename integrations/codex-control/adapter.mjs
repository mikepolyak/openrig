import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import * as fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SOURCE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const SEAT = /^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$/;
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const requireValue = (ok, message) => { if (!ok) throw new Error(message); };
const inside = (file, directory) => file === directory || file.startsWith(`${directory}${path.sep}`);

function absolute(value, label) {
  requireValue(typeof value === 'string' && path.isAbsolute(value), `${label} must be an absolute path`);
  return path.resolve(value);
}
function daemonUrl(value, label) {
  let u;
  try { u = new URL(value); } catch { throw new Error(`${label} must be an HTTP(S) origin`); }
  requireValue(['http:', 'https:'].includes(u.protocol) && !u.username && !u.password && !u.search && !u.hash && u.pathname === '/', `${label} must be an HTTP(S) origin without credentials, path, query or fragment`);
  return u.origin;
}
function privateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  requireValue(stat.isDirectory() && !stat.isSymbolicLink() && (stat.mode & 0o077) === 0, `Directory must be private (0700): ${directory}`);
}
function atomicJson(file, value, exclusive = false) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try { fs.writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  try {
    if (exclusive) fs.linkSync(temporary, file);
    else fs.renameSync(temporary, file);
    const dir = fs.openSync(path.dirname(file), 'r');
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

export function readBinding(bindingPath) {
  const file = absolute(bindingPath, 'binding');
  requireValue(!inside(file, SOURCE_ROOT), 'Private binding must be outside the source repository');
  const stat = fs.lstatSync(file);
  requireValue(stat.isFile() && !stat.isSymbolicLink() && (stat.mode & 0o077) === 0, 'Binding must be a regular private file (0600 or stricter)');
  const b = JSON.parse(fs.readFileSync(file, 'utf8'));
  requireValue(b.version === 1, 'Binding version must be 1');
  requireValue(ID.test(b.thread?.id ?? '') && ID.test(b.thread?.host ?? ''), 'thread.id and thread.host are required identifiers');
  b.cli = { node: absolute(b.cli?.node, 'cli.node'), path: absolute(b.cli?.path, 'cli.path') };
  fs.accessSync(b.cli.node, fs.constants.X_OK);
  requireValue(fs.statSync(b.cli.path).isFile(), 'cli.path must identify the built CLI file');
  b.local = { home: absolute(b.local?.home, 'local.home'), url: daemonUrl(b.local?.url, 'local.url'), session: b.local?.session };
  requireValue(SEAT.test(b.local.session ?? ''), 'local.session must be member@rig');
  requireValue(fs.statSync(b.local.home).isDirectory(), 'local.home must exist');
  b.stateDir = absolute(b.stateDir, 'stateDir');
  requireValue(!inside(b.stateDir, SOURCE_ROOT), 'Private stateDir must be outside the source repository');
  requireValue(b.targets && typeof b.targets === 'object' && !Array.isArray(b.targets) && Object.keys(b.targets).length > 0, 'targets must contain at least one named target');
  for (const [name, t] of Object.entries(b.targets)) {
    requireValue(ID.test(name) && t && typeof t === 'object', 'Invalid target');
    requireValue(SEAT.test(t.destination ?? ''), `targets.${name}.destination must be member@rig`);
    requireValue(t.host === undefined || ID.test(t.host), `targets.${name}.host must be a registered host identifier`);
    t.url = daemonUrl(t.url, `targets.${name}.url`);
  }
  b.timeoutMs ??= 30000;
  requireValue(Number.isInteger(b.timeoutMs) && b.timeoutMs >= 100 && b.timeoutMs <= 120000, 'timeoutMs must be an integer from 100 to 120000');
  return b;
}

export function runNativeProcess({ file, args, env, cwd, timeoutMs }) {
  return new Promise((resolve) => {
    execFile(file, args, { env, cwd, timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024, encoding: 'utf8' }, (error, stdout, stderr) => resolve({
      exitCode: error ? (typeof error.code === 'number' ? error.code : null) : 0,
      signal: error?.signal ?? null, timedOut: Boolean(error?.killed && error?.code == null),
      spawnError: error && typeof error.code === 'string' ? error.code : null,
      stdout: stdout ?? '', stderr: stderr ?? '', error: error?.message ?? null,
    }));
  });
}

function decode(native) { try { return JSON.parse(native.stdout); } catch { return null; } }
function rowFor(native, id) {
  const matches = [];
  const visit = (value) => {
    if (!value || typeof value !== 'object') return;
    if ((value.qitem_id ?? value.qitemId ?? value.id) === id && typeof value.state === 'string') matches.push(value);
    for (const child of Object.values(value)) if (child && typeof child === 'object') visit(child);
  };
  visit(decode(native));
  return matches.length === 1 ? matches[0] : null;
}
const destination = (row) => row?.destination_session ?? row?.destinationSession ?? row?.destination;
const succeeded = (native) => native.exitCode === 0 && !native.timedOut && !native.signal && !native.spawnError;

export async function execute(options, dependencies = {}) {
  const b = readBinding(options.binding);
  const run = dependencies.run ?? runNativeProcess;
  const ambient = dependencies.env ?? process.env;
  const base = Object.fromEntries(Object.entries(ambient).filter(([key]) => !key.startsWith('OPENRIG_')));
  const env = (url) => ({ ...base, OPENRIG_HOME: b.local.home, OPENRIG_URL: url, OPENRIG_SESSION_NAME: b.local.session });
  const invoke = (args, url = b.local.url) => run({ file: b.cli.node, args: [b.cli.path, ...args], env: env(url), cwd: path.dirname(b.cli.path), timeoutMs: b.timeoutMs });
  privateDirectory(b.stateDir);
  const receiptDir = path.join(b.stateDir, 'receipts');
  privateDirectory(receiptDir);
  const record = (operation, data) => {
    const file = path.join(receiptDir, `${Date.now()}-${randomUUID()}.json`);
    atomicJson(file, { version: 1, at: new Date().toISOString(), thread: b.thread, operation, ...data });
    return file;
  };
  const finish = (operation, native, extra = {}) => ({ ok: succeeded(native), operation, native, ...extra, receipt: record(operation, { native, ...extra }) });
  const id = options.id;
  const jobPath = () => { requireValue(ID.test(id ?? ''), 'id must be a safe, nonempty identifier'); return path.join(b.stateDir, 'jobs', id); };
  const loadJob = () => {
    const job = JSON.parse(fs.readFileSync(path.join(jobPath(), 'request.json'), 'utf8'));
    const target = b.targets[job.target];
    requireValue(target && target.destination === job.destination && (target.host ?? null) === job.host && b.local.session === job.source && b.local.home === job.sourceHome && b.thread.id === job.thread.id && b.thread.host === job.thread.host, 'Binding identity or target changed for this job');
    return { job, target };
  };
  const read = (operation, url = b.local.url) => invoke(['queue', operation, id, '--full', '--json'], url);

  if (options.command === 'submit') {
    const target = Object.hasOwn(b.targets, options.target ?? '') ? b.targets[options.target] : null;
    requireValue(target, 'submit requires a known --target');
    requireValue(typeof options.summary === 'string' && options.summary.trim(), 'submit requires --summary');
    const body = fs.readFileSync(absolute(options.bodyFile, 'body-file'));
    requireValue(body.length > 0, 'body-file must not be empty');
    const directory = jobPath();
    privateDirectory(path.dirname(directory)); privateDirectory(directory);
    const bodyHash = sha(body);
    const job = { version: 1, id, thread: b.thread, source: b.local.session, sourceHome: b.local.home, target: options.target, host: target.host ?? null, destination: target.destination, summary: options.summary, bodySha256: bodyHash };
    const manifest = path.join(directory, 'request.json');
    try { atomicJson(manifest, job, true); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    requireValue(JSON.stringify(JSON.parse(fs.readFileSync(manifest, 'utf8'))) === JSON.stringify(job), 'Conflicting reuse of job id: body, summary, source or target changed');
    const bodyPath = path.join(directory, `body-${bodyHash}.txt`);
    try {
      const fd = fs.openSync(bodyPath, 'wx', 0o600);
      try { fs.writeFileSync(fd, body); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    } catch (error) { if (error.code !== 'EEXIST') throw error; }
    requireValue(sha(fs.readFileSync(bodyPath)) === bodyHash, 'Persisted body snapshot does not match request hash');
    const args = ['queue', 'create', '--id', id, '--source', b.local.session, '--destination', target.destination, '--body-file', bodyPath, '--summary', options.summary, '--json', '--verify'];
    if (target.host) args.push('--host', target.host);
    const native = await invoke(args);
    if (succeeded(native)) return finish('submit', native, { jobId: id, outcome: 'native-receipt', bodySha256: bodyHash });
    const reconciliation = await read('show', target.url);
    const row = succeeded(reconciliation) ? rowFor(reconciliation, id) : null;
    const matched = row && destination(row) === job.destination && typeof row.body === 'string' && sha(Buffer.from(row.body)) === bodyHash;
    return finish('submit', native, { ok: Boolean(matched), jobId: id, outcome: matched ? 'reconciled-existing-row' : 'indeterminate', bodySha256: bodyHash, reconciliation });
  }
  if (options.command === 'status' || options.command === 'transitions') {
    const { target } = loadJob();
    return finish(options.command, await read(options.command === 'status' ? 'show' : 'transitions', target.url), { jobId: id });
  }
  if (options.command === 'inbox') {
    const limit = options.limit === undefined ? 100 : Number(options.limit);
    requireValue(Number.isInteger(limit) && limit >= 1 && limit <= 10000, 'limit must be an integer from 1 to 10000');
    return finish('inbox', await invoke(['queue', 'list', '--destination', b.local.session, '--all', '--full', '--json', '--limit', String(limit)]), { limit, completeness: 'bounded-native-list' });
  }
  if (options.command === 'accept') {
    jobPath();
    const resolution = fs.readFileSync(absolute(options.resolutionFile, 'resolution-file'), 'utf8').trim();
    requireValue(resolution.length > 0, 'resolution-file must not be empty');
    let observed = await read('show');
    let row = succeeded(observed) ? rowFor(observed, id) : null;
    requireValue(row && destination(row) === b.local.session, 'Cannot accept: native row is unavailable, ambiguous or owned by another destination');
    if (['done', 'failed', 'denied', 'canceled', 'handed-off'].includes(row.state)) return finish('accept', observed, { jobId: id, outcome: 'already-terminal-no-change' });
    if (row.state === 'pending') {
      const claim = await invoke(['queue', 'claim', id, '--full', '--json']);
      record('claim', { jobId: id, native: claim });
      if (!succeeded(claim)) return finish('accept', claim, { jobId: id, outcome: 'claim-not-confirmed' });
    } else requireValue(row.state === 'in-progress', 'Cannot accept a blocked or unknown-state row');
    const args = ['queue', 'update', id, '--state', 'done', '--closure-reason', 'no-follow-on', '--resolution', resolution, '--note', 'Result accepted by the bound control session', '--full', '--json'];
    if (options.evidenceRef) args.push('--evidence-ref', options.evidenceRef);
    const native = await invoke(args);
    return finish('accept', native, { jobId: id, outcome: succeeded(native) ? 'native-close-receipt' : 'close-indeterminate', resolutionSha256: sha(resolution) });
  }
  throw new Error('Command must be submit, status, transitions, inbox or accept');
}
