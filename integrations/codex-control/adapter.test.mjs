import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execute, readBinding } from './adapter.mjs';

const success = (value = { persisted: true }) => ({ exitCode: 0, signal: null, timedOut: false, spawnError: null, stdout: JSON.stringify(value), stderr: '', error: null });
const timeout = () => ({ exitCode: null, signal: 'SIGTERM', timedOut: true, spawnError: null, stdout: '', stderr: 'response lost', error: 'Command timed out' });
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-control-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'runtime'); fs.mkdirSync(home, { mode: 0o700 });
  const cliPath = path.join(root, 'native-cli.js'); fs.writeFileSync(cliPath, '// test fixture\n');
  const binding = path.join(root, 'binding.json');
  const config = { version: 1, thread: { id: 'thread-1', host: 'local' }, cli: { node: process.execPath, path: cliPath }, local: { home, url: 'http://127.0.0.1:7433', session: 'desktop-control@team' }, stateDir: path.join(root, 'private-state'), targets: { remote: { host: 'worker-host', url: 'http://127.0.0.1:17433', destination: 'audit-user@workers' } } };
  const save = () => fs.writeFileSync(binding, JSON.stringify(config), { mode: 0o600 }); save();
  const bodyFile = path.join(root, 'brief.txt');
  fs.writeFileSync(bodyFile, 'Audit only. Literal: $(echo unsafe) `touch unsafe`\nSecond line.');
  const options = { binding, command: 'submit', id: 'job-1', target: 'remote', bodyFile, summary: 'Audit a user flow' };
  return { root, binding, config, save, options, body: fs.readFileSync(bodyFile, 'utf8') };
}
const flag = (args, name) => args[args.indexOf(name) + 1];

test('submit seals identity and body before spawning the native CLI, with explicit caller/routing and no shell interpolation', async (t) => {
  const f = fixture(t); const calls = [];
  const result = await execute(f.options, { env: { PATH: '/bin', OPENRIG_URL: 'http://wrong.invalid', OPENRIG_SESSION_NAME: 'wrong@seat', OPENRIG_HOST: 'wrong' }, run: async (call) => {
    calls.push(call);
    const job = JSON.parse(fs.readFileSync(path.join(f.config.stateDir, 'jobs/job-1/request.json')));
    assert.equal(job.thread.id, 'thread-1');
    assert.equal(job.bodySha256, createHash('sha256').update(f.body).digest('hex'));
    assert.equal(fs.readFileSync(flag(call.args, '--body-file'), 'utf8'), f.body);
    return success();
  } });
  assert.equal(result.ok, true); assert.equal(result.outcome, 'native-receipt');
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.file, process.execPath); assert.equal(call.args[0], f.config.cli.path);
  assert.deepEqual(call.args.slice(1, 3), ['queue', 'create']);
  assert.equal(flag(call.args, '--source'), 'desktop-control@team');
  assert.equal(flag(call.args, '--destination'), 'audit-user@workers');
  assert.equal(flag(call.args, '--host'), 'worker-host');
  assert.equal(call.env.OPENRIG_URL, f.config.local.url);
  assert.equal(call.env.OPENRIG_HOME, f.config.local.home);
  assert.equal(call.env.OPENRIG_SESSION_NAME, f.config.local.session);
  assert.equal(call.env.OPENRIG_HOST, undefined);
  assert.equal(call.env.PATH, '/bin');
  assert.ok(!Object.hasOwn(call, 'shell'));
  assert.equal(fs.statSync(result.receipt).mode & 0o777, 0o600);
});

test('identical retry reuses the persisted native task ID and body snapshot', async (t) => {
  const f = fixture(t); const calls = [];
  const run = async (call) => { calls.push(call); return success(); };
  await execute(f.options, { run });
  const copiedBody = path.join(f.root, 'same-body.txt'); fs.writeFileSync(copiedBody, f.body);
  await execute({ ...f.options, bodyFile: copiedBody }, { run });
  assert.deepEqual(calls.map((c) => flag(c.args, '--id')), ['job-1', 'job-1']);
  assert.equal(flag(calls[0].args, '--body-file'), flag(calls[1].args, '--body-file'));
  assert.equal(fs.readdirSync(path.join(f.config.stateDir, 'jobs')).length, 1);
});

test('conflicting body or summary cannot reuse an existing ID or invoke the CLI', async (t) => {
  const f = fixture(t); let calls = 0;
  const run = async () => { calls++; return success(); };
  await execute(f.options, { run });
  await assert.rejects(execute({ ...f.options, summary: 'Different work' }, { run }), /Conflicting reuse/);
  fs.writeFileSync(f.options.bodyFile, 'Different body');
  await assert.rejects(execute(f.options, { run }), /Conflicting reuse/);
  assert.equal(calls, 1);
});

test('uncertain submit reconciles the same ID on the receiving daemon without --host', async (t) => {
  const f = fixture(t); const calls = [];
  const result = await execute(f.options, { run: async (call) => {
    calls.push(call);
    return calls.length === 1 ? timeout() : success({ item: { qitem_id: 'job-1', state: 'in-progress', destination_session: 'audit-user@workers', body: f.body } });
  } });
  assert.equal(result.ok, true); assert.equal(result.outcome, 'reconciled-existing-row');
  assert.equal(result.native.timedOut, true);
  assert.deepEqual(calls[1].args.slice(1), ['queue', 'show', 'job-1', '--full', '--json']);
  assert.equal(calls[1].env.OPENRIG_URL, f.config.targets.remote.url);
  assert.ok(!calls[1].args.includes('--host'));
  assert.equal(JSON.parse(result.reconciliation.stdout).item.state, 'in-progress');
});

test('unavailable or conflicting reconciliation stays indeterminate', async (t) => {
  const f = fixture(t);
  for (const native of [timeout(), success({ qitem_id: 'job-1', state: 'done', destination_session: 'other@workers', body: f.body }), success({ qitem_id: 'job-1', state: 'done', destination_session: 'audit-user@workers', body: 'different' })]) {
    let calls = 0;
    const result = await execute(f.options, { run: async () => ++calls === 1 ? timeout() : native });
    assert.equal(result.ok, false); assert.equal(result.outcome, 'indeterminate'); assert.equal(calls, 2);
  }
});

test('status/transitions follow receiver URL, permit tunnel URL changes, and never add unsupported --host', async (t) => {
  const f = fixture(t); await execute(f.options, { run: async () => success() });
  f.config.targets.remote.url = 'http://127.0.0.1:27433'; f.save();
  for (const [command, verb] of [['status', 'show'], ['transitions', 'transitions']]) {
    let call;
    await execute({ binding: f.binding, command, id: 'job-1' }, { run: async (c) => { call = c; return success({ native: 'unchanged' }); } });
    assert.deepEqual(call.args.slice(1), ['queue', verb, 'job-1', '--full', '--json']);
    assert.equal(call.env.OPENRIG_URL, f.config.targets.remote.url);
  }
});

test('changed target identity is refused before reading a different owner queue', async (t) => {
  const f = fixture(t); await execute(f.options, { run: async () => success() });
  f.config.targets.remote.destination = 'different@workers'; f.save();
  await assert.rejects(execute({ binding: f.binding, command: 'status', id: 'job-1' }, { run: async () => assert.fail('must not spawn') }), /identity or target changed/);
});

test('inbox is explicit, bounded and local', async (t) => {
  const f = fixture(t); let call;
  const result = await execute({ binding: f.binding, command: 'inbox', limit: '25' }, { run: async (c) => { call = c; return success([]); } });
  assert.equal(call.env.OPENRIG_URL, f.config.local.url);
  assert.equal(flag(call.args, '--destination'), f.config.local.session);
  assert.equal(flag(call.args, '--limit'), '25');
  assert.ok(call.args.includes('--all')); assert.equal(result.completeness, 'bounded-native-list');
});

test('accept verifies recipient, claims pending result and closes with literal resolution', async (t) => {
  const f = fixture(t); const resolutionFile = path.join(f.root, 'resolution.txt'); fs.writeFileSync(resolutionFile, 'Reviewed evidence.\nLiteral $(never-run).');
  const calls = [];
  const result = await execute({ binding: f.binding, command: 'accept', id: 'result-1', resolutionFile, evidenceRef: 'runs/report.md' }, { run: async (call) => {
    calls.push(call);
    return calls.length === 1 ? success({ qitem_id: 'result-1', state: 'pending', destination_session: f.config.local.session }) : success();
  } });
  assert.equal(result.ok, true); assert.equal(result.outcome, 'native-close-receipt');
  assert.deepEqual(calls.map((c) => c.args[2]), ['show', 'claim', 'update']);
  assert.equal(flag(calls[2].args, '--resolution'), 'Reviewed evidence.\nLiteral $(never-run).');
  assert.equal(flag(calls[2].args, '--closure-reason'), 'no-follow-on');
  assert.equal(flag(calls[2].args, '--evidence-ref'), 'runs/report.md');
  assert.ok(calls.every((c) => c.env.OPENRIG_SESSION_NAME === f.config.local.session));
});

test('accept cannot mutate another recipient, blocked result or an ambiguous record', async (t) => {
  const f = fixture(t); const resolutionFile = path.join(f.root, 'resolution.txt'); fs.writeFileSync(resolutionFile, 'Checked');
  for (const data of [{ qitem_id: 'result-1', state: 'pending', destination_session: 'other@team' }, { qitem_id: 'result-1', state: 'blocked', destination_session: f.config.local.session }, { rows: [{ qitem_id: 'result-1', state: 'pending' }, { qitem_id: 'result-1', state: 'pending' }] }]) {
    let calls = 0;
    await assert.rejects(execute({ binding: f.binding, command: 'accept', id: 'result-1', resolutionFile }, { run: async () => { calls++; return success(data); } }), /Cannot accept/);
    assert.equal(calls, 1);
  }
});

test('already terminal acceptance is read-only and never claims a fresh acceptance', async (t) => {
  const f = fixture(t); const resolutionFile = path.join(f.root, 'resolution.txt'); fs.writeFileSync(resolutionFile, 'Checked');
  let calls = 0;
  const result = await execute({ binding: f.binding, command: 'accept', id: 'result-1', resolutionFile }, { run: async () => { calls++; return success({ qitem_id: 'result-1', state: 'done', destination_session: f.config.local.session }); } });
  assert.equal(calls, 1); assert.equal(result.outcome, 'already-terminal-no-change');
});

test('private binding and required fields are enforced before spawning', async (t) => {
  const f = fixture(t);
  fs.chmodSync(f.binding, 0o644); assert.throws(() => readBinding(f.binding), /private file/);
  fs.chmodSync(f.binding, 0o600); delete f.config.thread; f.save();
  assert.throws(() => readBinding(f.binding), /thread.id/);
  f.config.thread = { id: 'thread-1', host: 'local' }; f.config.local.url = 'https://user:secret@example.test'; f.save();
  assert.throws(() => readBinding(f.binding), /without credentials/);
});

test('path traversal IDs and inherited target names are refused', async (t) => {
  const f = fixture(t); const run = async () => assert.fail('must not spawn');
  await assert.rejects(execute({ ...f.options, id: '../escape' }, { run }), /safe, nonempty/);
  await assert.rejects(execute({ ...f.options, target: 'toString' }, { run }), /known --target/);
});

test('native nonzero and spawn diagnostics are retained without inventing completion', async (t) => {
  const f = fixture(t); await execute(f.options, { run: async () => success() });
  const error = { exitCode: null, signal: null, timedOut: false, spawnError: 'ENOENT', stdout: '', stderr: 'native diagnostic', error: 'missing executable' };
  const result = await execute({ binding: f.binding, command: 'status', id: 'job-1' }, { run: async () => error });
  assert.equal(result.ok, false); assert.deepEqual(result.native, error);
  assert.deepEqual(JSON.parse(fs.readFileSync(result.receipt)).native, error);
  assert.ok(fs.readdirSync(path.dirname(result.receipt)).every((name) => name.endsWith('.json')));
});
