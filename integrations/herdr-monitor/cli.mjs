#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { readBinding, synchronize, watch } from './monitor.mjs';

const usage = 'node integrations/herdr-monitor/cli.mjs --binding /absolute/private/monitor.json [--dry-run] once|watch';
try {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    binding: { type: 'string' }, 'dry-run': { type: 'boolean' }, help: { type: 'boolean', short: 'h' }
  } });
  if (values.help) console.log(usage);
  else {
    if (!values.binding || positionals.length !== 1 || !['once', 'watch'].includes(positionals[0])) throw new Error(usage);
    const read = () => readBinding(values.binding);
    const tick = (config) => synchronize(config, { dryRun: Boolean(values['dry-run']) });
    const emit = (value) => console.log(JSON.stringify(value));
    if (positionals[0] === 'once') {
      const result = await tick(read());
      emit(result);
      if (!result.ok) process.exitCode = 1;
    } else {
      const controller = new AbortController();
      process.once('SIGINT', () => controller.abort());
      process.once('SIGTERM', () => controller.abort());
      await watch({ read, tick, emit, signal: controller.signal });
    }
  }
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message }));
  process.exitCode = 1;
}
