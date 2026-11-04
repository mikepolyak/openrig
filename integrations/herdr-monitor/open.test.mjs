import assert from 'node:assert/strict';
import test from 'node:test';
import { bindViewer } from './open.mjs';

const target = { matchLabel: 'Remote viewer', tmuxSession: 'owned-viewer', readOnly: true, kind: 'worker', label: 'Remote' };
const config = { tmux: '/bin/tmux', tmuxSocket: '/external drive/run/socket' };
const pane = { workspace_id: 'w1', pane_id: 'w1:p2', terminal_id: 'terminal-2', label: 'Remote viewer' };
const processInfo = { pane_id: pane.pane_id, foreground_processes: [{ argv: ['/bin/tmux', '-S', config.tmuxSocket, 'attach', '-r', '-t', '$9'] }] };
const session = { id: '$9', name: 'owned-viewer' };
const bind = (panes = [pane], info = processInfo, actualSession = session) => bindViewer(target, 'w1', panes, info, actualSession, config);

test('binds only the exact returned workspace, terminal and read-only tmux session', () => {
  const binding = bind();
  assert.equal(binding.paneId, pane.pane_id);
  assert.equal(binding.terminalId, pane.terminal_id);
  assert.deepEqual(binding.foregroundArgv, processInfo.foreground_processes[0].argv);
  assert.equal(binding.matchLabel, undefined);
});

test('refuses duplicate or unrelated viewer panes', () => {
  assert.throws(() => bind([pane, pane]), /one pane/);
  assert.throws(() => bind([{ ...pane, workspace_id: 'other' }]), /one pane/);
});

test('refuses another socket, changed session ID/name, or writable remote attach', () => {
  for (const replacement of [
    ['/bin/tmux', '-S', '/other/socket', 'attach', '-r', '-t', '$9'],
    ['/bin/tmux', '-S', config.tmuxSocket, 'attach', '-r', '-t', '$8'],
    ['/bin/tmux', '-S', config.tmuxSocket, 'attach', '-t', '$9']
  ]) assert.throws(() => bind([pane], { ...processInfo, foreground_processes: [{ argv: replacement }] }), /declared session/);
  assert.throws(() => bind([pane], processInfo, { ...session, name: 'another-seat' }), /declared session/);
});
