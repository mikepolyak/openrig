#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { execute } from './adapter.mjs';

const usage = `Codex control adapter (local CLI; no daemon or provider launch)

node integrations/codex-control/cli.mjs --binding /private/binding.json <command>
  submit --id ID --target NAME --body-file FILE --summary TEXT
  status --id ID
  transitions --id ID
  inbox [--limit N]
  accept --id RESULT_ID --resolution-file FILE [--evidence-ref PATH]

Binding and receipts stay outside this source repository. Native JSON receipts
are returned unchanged inside the native/reconciliation fields. A successful
submit is a queue receipt, not proof of worker pickup or completion.
`;
try {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    binding: { type: 'string' }, id: { type: 'string' }, target: { type: 'string' },
    'body-file': { type: 'string' }, summary: { type: 'string' },
    'resolution-file': { type: 'string' }, 'evidence-ref': { type: 'string' },
    limit: { type: 'string' }, help: { type: 'boolean', short: 'h' },
  } });
  if (values.help) process.stdout.write(usage);
  else {
    if (positionals.length !== 1 || !values.binding) throw new Error(usage);
    const result = await execute({ ...values, command: positionals[0], bodyFile: values['body-file'], resolutionFile: values['resolution-file'], evidenceRef: values['evidence-ref'] });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (!result.ok) process.exitCode = 1;
  }
} catch (error) {
  process.stderr.write(`${JSON.stringify({ ok: false, error: error.message, code: error.code ?? null })}\n`);
  process.exitCode = 1;
}
