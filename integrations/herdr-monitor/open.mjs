#!/usr/bin/env node
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readBinding, synchronize } from './monitor.mjs';

const execute = promisify(execFile);
const directory = path.dirname(fileURLToPath(import.meta.url));
const assert = (condition, message) => { if (!condition) throw new Error(message); };

export function bindViewer(target, workspaceId, panes, processInfo, session, config) {
  const matches = panes.filter(p => p.workspace_id === workspaceId && p.label === target.matchLabel);
  assert(matches.length === 1, `Expected one pane for ${target.matchLabel}`);
  const pane = matches[0];
  const processes = processInfo.foreground_processes;
  assert(processInfo.pane_id === pane.pane_id && processes?.length === 1, 'Ambiguous viewer process');
  const argv = processes[0].argv;
  const socket = argv.indexOf('-S');
  const attach = argv.indexOf('attach');
  const dest = argv.indexOf('-t');
  assert(argv[0] === config.tmux && socket === 1 && argv[socket + 1] === config.tmuxSocket
    && attach === 3 && dest > attach && argv[dest + 1] === session.id
    && session.name === target.tmuxSession && argv.includes('-r') === (target.readOnly === true), 'Viewer does not attach to the declared session');
  const { matchLabel, tmuxSession, readOnly, ...observation } = target;
  return { ...observation, workspaceId, paneId: pane.pane_id, terminalId: pane.terminal_id, foregroundArgv: argv };
}

function atomicJson(filename, value) {
  const temporary = filename + '.' + process.pid + '.tmp';
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value, null, 2) + '\n'); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  fs.renameSync(temporary, filename);
}

async function main(filename) {
  assert(filename && path.isAbsolute(filename), 'Use an absolute private launcher configuration');
  const stat = fs.lstatSync(filename);
  assert(stat.isFile() && !stat.isSymbolicLink() && (stat.mode & 0o077) === 0, 'Launcher config must be a private regular file');
  const config = JSON.parse(fs.readFileSync(filename, 'utf8'));
  assert(config.version === 1 && Array.isArray(config.viewers) && config.viewers.length, 'Invalid launcher configuration');
  for (const key of ['rig', 'herdr', 'tmux', 'tmuxSocket', 'bindingPath']) assert(path.isAbsolute(config[key] ?? ''), `Expected absolute ${key}`);
  const command = async (file, args, timeout = 20000) => (await execute(file, args, { timeout, maxBuffer: 4 * 1024 * 1024 })).stdout;
  const json = async (file, args, timeout) => {
    const output = await command(file, args, timeout);
    const line = output.trim().split('\n').findLast(l => l.startsWith('{'));
    assert(line, 'Command returned no JSON result');
    return JSON.parse(line);
  };
  const herdr = async (args) => (await json(config.herdr, ['--session', config.session, ...args])).result;
  const opened = await json(config.rig, ['terminal', 'open', config.view, '--window', '--json'], 180000);
  assert(opened.ok && opened.provider === 'herdr', 'OpenRig could not open the Herdr view');
  const workspaceId = opened.workspaceId ?? opened.reusedWorkspace?.id;
  assert(typeof workspaceId === 'string', 'OpenRig returned no workspace identity');
  const panes = (await herdr(['pane', 'list', '--workspace', workspaceId])).panes;
  const viewers = [];
  for (const target of config.viewers) {
    const matching = panes.filter(p => p.workspace_id === workspaceId && p.label === target.matchLabel);
    assert(matching.length === 1, 'Missing or ambiguous monitor pane');
    const processInfo = (await herdr(['pane', 'process-info', '--pane', matching[0].pane_id])).process_info;
    const sessionLine = (await command(config.tmux, ['-S', config.tmuxSocket, 'display-message', '-p', '-t', '=' + target.tmuxSession + ':', '#{session_id} #{session_name}'])).trim();
    const split = sessionLine.indexOf(' ');
    viewers.push(bindViewer(target, workspaceId, panes, processInfo, { id: sessionLine.slice(0, split), name: sessionLine.slice(split + 1) }, config));
  }
  const binding = { version: 1, rig: config.rig, herdr: config.herdr, session: config.session, localUrl: config.localUrl, intervalMs: config.intervalMs ?? 5000, maxAgeMs: config.maxAgeMs ?? 30000, viewers };
  atomicJson(config.bindingPath, binding);
  const verified = readBinding(config.bindingPath);
  const result = await synchronize(verified);
  assert(result.ok, 'Monitoring metadata could not be verified');
  const statePath = config.bindingPath + '.watch.json';
  let running = false;
  if (fs.existsSync(statePath)) {
    const previous = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    try {
      const processCommand = await command('/bin/ps', ['-p', String(previous.pid), '-o', 'command=']);
      running = processCommand.trim() === previous.command;
    } catch { /* The prior watcher exited. Its expiring metadata has no permanent state. */ }
  }
  if (!running) {
    const cli = path.join(directory, 'cli.mjs');
    const args = [cli, '--binding', config.bindingPath, 'watch'];
    const log = fs.openSync(config.bindingPath + '.watch.log', 'a', 0o600);
    const child = spawn(process.execPath, args, { detached: true, stdio: ['ignore', log, log] });
    fs.closeSync(log);
    child.unref();
    await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    const processCommand = (await command('/bin/ps', ['-p', String(child.pid), '-o', 'command='])).trim();
    atomicJson(statePath, { pid: child.pid, command: processCommand });
  }
  console.log(JSON.stringify({ ok: true, workspaceId, viewers: result.viewers ?? result.results, binding: config.bindingPath }));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv[2]).catch(error => { console.error(JSON.stringify({ ok: false, error: error.message })); process.exitCode = 1; });
}
