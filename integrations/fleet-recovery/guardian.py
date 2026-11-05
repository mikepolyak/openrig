#!/usr/bin/env python3
"""One-shot, allowlisted OpenRig recovery. No stops, fresh sessions or TCC workarounds."""
import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Held(RuntimeError):
    pass


def run(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # CLI output can include session identifiers or source material. Never log it here.
        raise Held(f'command failed ({result.returncode}): {argv[0]} {argv[1]}')
    return result.stdout


def json_command(argv, timeout=30):
    try:
        return json.loads(run(argv, timeout))
    except (ValueError, subprocess.SubprocessError) as error:
        raise Held(f'command did not return complete JSON: {type(error).__name__}') from error


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def process(pid):
    if type(pid) is not int or pid <= 1:
        raise Held('invalid recorded PID')
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,command='],
                            capture_output=True, text=True, timeout=5)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode:
        raise Held('process identity unavailable')
    parts = result.stdout.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        raise Held('process identity malformed')
    return int(parts[0]), parts[1]


def validate_health(health, state, config, process_read=None):
    if (health.get('status') != 'ok' or health.get('euid') != config['uid']
            or health.get('selfHostId') != config['hostId']
            or health.get('pid') != state.get('pid')
            or state.get('db') != str(Path(config['runtime']) / 'openrig.sqlite')
            or health.get('bind', {}).get('hosts') != ['127.0.0.1']):
        raise Held('daemon identity, database or loopback policy mismatch')
    observed = (process_read or process)(health['pid'])
    expected = f"{config['nodeBinary']} {config['daemonScript']}"
    if observed != (config['uid'], expected):
        raise Held('daemon process identity mismatch')


def health_read(config):
    try:
        with urllib.request.urlopen('http://127.0.0.1:7433/healthz', timeout=3) as response:
            return json.load(response)
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError) or getattr(error.reason, 'errno', None) == errno.ECONNREFUSED:
            return None
        raise Held('daemon reachability unknown; preserved') from error
    except (ValueError, OSError) as error:
        raise Held('daemon health unreadable; preserved') from error


def validate_config(config):
    required = ('runtime', 'volume', 'volumeUuid', 'uid', 'hostId', 'rig', 'nodeBinary', 'daemonScript', 'seats')
    if any(key not in config for key in required) or config['uid'] != os.getuid():
        raise Held('missing configuration or user mismatch')
    for key in ('runtime', 'volume', 'rig', 'nodeBinary', 'daemonScript'):
        if not Path(config[key]).is_absolute():
            raise Held(f'configuration path is not absolute: {key}')
    if not Path(config['runtime']).is_relative_to(Path(config['volume'])):
        raise Held('runtime is outside selected volume')
    if not isinstance(config['seats'], list) or not config['seats']:
        raise Held('empty seat allowlist')
    identities = set()
    for seat in config['seats']:
        if not all(isinstance(seat.get(k), str) and seat[k] for k in ('rigId', 'nodeId', 'logicalId', 'session', 'runtime')):
            raise Held('incomplete allowlisted seat identity')
        identity = (seat['rigId'], seat['logicalId'])
        if identity in identities or seat['runtime'] not in ('codex', 'claude-code'):
            raise Held('duplicate or unsupported seat')
        identities.add(identity)


def verify_volume(config):
    try:
        info = plistlib.loads(run(['/usr/sbin/diskutil', 'info', '-plist', config['volume']], 10).encode())
    except (ValueError, plistlib.InvalidFileException) as error:
        raise Held('volume identity unreadable') from error
    if info.get('VolumeUUID') != config['volumeUuid'] or info.get('MountPoint') != config['volume']:
        raise Held('external volume identity or mount mismatch')
    if not Path(config['runtime']).is_dir():
        raise Held('runtime unavailable')


def owned_work(rows, session, count):
    if not isinstance(rows, list) or len(rows) != count or len(rows) >= 101:
        raise Held('active task ownership is incomplete')
    claims = []
    for row in rows:
        if (row.get('destinationSession') != session or row.get('state') != 'in-progress'
                or not row.get('qitemId') or not row.get('claimedAt') or row.get('blockedOn')
                or row.get('humanQuestions')):
            raise Held('active task ownership or unattended eligibility mismatch')
        claims.append((row['qitemId'], row['claimedAt']))
    if len(set(item[0] for item in claims)) != len(claims):
        raise Held('duplicate active task identity')
    return sorted(claims)


def seat_decision(row, expected, now=None, max_age=90, allow_in_progress=False, owned_rows=None):
    """Only fresh, positive executor absence is eligible; activity idle is not absence."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(row, dict):
        return 'hold', 'seat absent or ambiguous'
    identities = {'rigId': 'rigId', 'logicalId': 'logicalId', 'canonicalSessionName': 'session', 'runtime': 'runtime'}
    if any(row.get(key) != expected[value] for key, value in identities.items()) or row.get('nodeId') != expected['nodeId']:
        return 'hold', 'seat identity mismatch'
    if row.get('hostSelfId') != expected['hostId']:
        return 'hold', 'seat host mismatch'
    if row.get('intentionallyStopped') is True:
        return 'preserve', 'stopped on purpose'
    if row.get('intentionallyStopped') is not False:
        return 'hold', 'stop intent unknown'
    verdict = row.get('identityVerdict') or {}
    if row.get('sessionStatus') == 'running' and verdict.get('verdict') == 'verified':
        return 'preserve', 'live'
    if row.get('sessionStatus') not in ('exited', 'detached') or verdict.get('reason') not in ('session_missing', 'pane_exited'):
        return 'hold', 'executor absence unconfirmed'
    try:
        sampled = dt.datetime.fromisoformat(verdict['observedAt'].replace('Z', '+00:00'))
        age = (now - sampled).total_seconds()
    except (KeyError, ValueError, TypeError):
        return 'hold', 'identity sample missing'
    if age < -5 or age > max_age:
        return 'hold', 'identity sample stale'
    if row.get('terminalActive') is not False:
        return 'hold', 'terminal absence unconfirmed'
    if row.get('heldReason'):
        return 'hold', 'seat is held'
    if type(row.get('blockedWorkCount')) is not int or row['blockedWorkCount'] != 0:
        return 'hold', 'blocked work requires reconciliation'
    needs_input = row.get('arbitratedNeedsInput')
    if needs_input and (not isinstance(needs_input, dict) or needs_input.get('count', 1) != 0):
        return 'hold', 'seat needs input'
    count = row.get('inProgressWorkCount')
    if type(count) is not int or count < 0:
        return 'hold', 'active work count unknown'
    if count:
        if allow_in_progress is not True:
            return 'hold', 'active work recovery is not enabled'
        try:
            owned_work(owned_rows, expected['session'], count)
        except Held as error:
            return 'hold', str(error)
    if not row.get('resumeToken') or row.get('resumeType') not in ('codex_id', 'claude_session_id', 'claude_code_session_id'):
        return 'hold', 'verified continuity source unavailable'
    return 'recover', 'confirmed unintentional exit'


def selected_snapshot(plan):
    if not isinstance(plan, dict) or plan.get('ok') is not True or plan.get('planOnly') is not True:
        raise Held('single-seat preview unavailable')
    selection = plan.get('snapshotSelection')
    if not isinstance(selection, dict) or not isinstance(selection.get('snapshotId'), str) or not selection['snapshotId']:
        raise Held('single-seat snapshot selection unavailable')
    return selection['snapshotId']


def validate_plan(plan, resume_plan, expected):
    snapshot = selected_snapshot(plan)
    targets = plan.get('targetNodes')
    if targets != [{'nodeId': expected['nodeId'], 'logicalId': expected['logicalId']}]:
        raise Held('preview target mismatch')
    effects = plan.get('nonTargetEffects', {})
    if not isinstance(effects, dict) or effects.get('mode') != 'unchanged' or effects.get('affected') != []:
        raise Held('preview would change siblings')
    if not isinstance(resume_plan, dict) or resume_plan.get('status') != 'plan':
        raise Held('resume preview unavailable')
    resume_snapshot = resume_plan.get('snapshot')
    if not isinstance(resume_snapshot, dict) or resume_snapshot.get('id') != snapshot:
        raise Held('resume preview snapshot mismatch')
    forecast_nodes = resume_plan.get('nodes')
    if not isinstance(forecast_nodes, list) or any(not isinstance(n, dict) for n in forecast_nodes):
        raise Held('resume preview nodes malformed')
    nodes = [n for n in forecast_nodes if n.get('logicalId') == expected['logicalId']]
    if len(nodes) != 1 or nodes[0].get('intendedAction') != 'resume-original' or nodes[0].get('tokenState') != 'present' or nodes[0].get('freshRequired') is not False:
        raise Held('original conversation is not verified resumable')
    return snapshot


def inventory(config):
    rows = json_command([config['rig'], 'ps', '--nodes', '-A', '--json', '--full', '--no-cleanup'])
    if not isinstance(rows, list):
        raise Held('inventory is incomplete or malformed')
    return rows


def resume_preview(rig_id):
    request = urllib.request.Request(
        'http://127.0.0.1:7433/api/rigs/' + urllib.parse.quote(rig_id, safe='') + '/launch-plan',
        data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise Held('read-only resume forecast unavailable') from error


def matching(rows, expected):
    matches = [row for row in rows if row.get('rigId') == expected['rigId'] and row.get('logicalId') == expected['logicalId']]
    return matches[0] if len(matches) == 1 else None


def task_ownership(config, row, expected):
    if (not row or not row.get('inProgressWorkCount')
            or config.get('allowInProgressRecovery') is not True):
        return None
    return json_command([config['rig'], 'queue', 'list', '-A', '--destination', expected['session'],
                         '--state', 'in-progress,blocked', '--limit', '101', '--full', '--json'])


def original_running(row, before, expected):
    return bool(row and row.get('sessionStatus') == 'running'
                and row.get('rigId') == expected['rigId'] and row.get('nodeId') == expected['nodeId']
                and row.get('logicalId') == expected['logicalId']
                and row.get('canonicalSessionName') == expected['session']
                and row.get('hostSelfId') == expected['hostId']
                and row.get('runtime') == expected['runtime']
                and row.get('intentionallyStopped') is False
                and row.get('resumeToken') == before.get('resumeToken')
                and row.get('resumeType') == before.get('resumeType')
                and (row.get('identityVerdict') or {}).get('verdict') == 'verified')


def exclusive_json(path, value):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())
    return True


def occupant_epoch(row):
    epoch = row.get('startupCompletedAt') or (row.get('modelObservation') or {}).get('generation')
    if not isinstance(epoch, str) or not epoch:
        raise Held('durable original occupant epoch missing')
    return epoch


def launch_attempt(config, expected, before, snapshot_id, claims):
    token_hash = hashlib.sha256(before['resumeToken'].encode()).hexdigest()
    epoch = occupant_epoch(before)
    key = hashlib.sha256(json.dumps([expected, token_hash, epoch], sort_keys=True).encode()).hexdigest()
    path = Path(config['runtime']) / 'run' / f'guardian-launch-{key}.json'
    receipt = {'state': 'attempting', 'attemptId': key, 'seat': expected['session'],
               'rigId': expected['rigId'], 'nodeId': expected['nodeId'],
               'originalOccupantEpoch': epoch, 'resumeTokenSha256': token_hash,
               'snapshotId': snapshot_id, 'originalClaims': claims,
               'observedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    for prior_path in (Path(config['runtime'])/'run').glob('guardian-launch-*.json'):
        prior = json.loads(prior_path.read_text())
        if (prior_path != path and prior.get('nodeId') == expected['nodeId']
                and prior.get('resumeTokenSha256') == token_hash
                and prior.get('state') in ('attempting', 'pending-reconciliation')):
            raise Held('prior original occupant launch is unresolved; no new apply')
    created = exclusive_json(path, receipt)
    if not created:
        receipt = json.loads(path.read_text())
        if (receipt.get('resumeTokenSha256') != token_hash or receipt.get('originalOccupantEpoch') != epoch
                or receipt.get('nodeId') != expected['nodeId']):
            raise Held('durable launch attempt identity mismatch')
    return path, receipt, created


def reconcile_launch(config, expected, before):
    """An ambiguous command is never retried. Observe the exact occupant for 20s."""
    deadline = time.monotonic() + 20
    while True:
        try:
            current = matching(inventory(config), expected)
            if original_running(current, before, expected):
                return current
        except (Held, OSError, ValueError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            raise Held('resumed original occupant not verified within reconciliation window; no retry')
        time.sleep(0.5)


def wake_owned_work(config, expected, before, original_claims):
    """One guarded continuation wake; durable attempt prevents ambiguous resend."""
    if not original_claims:
        return {'state': 'not-needed', 'taskConsumption': 'not-applicable'}
    current = matching(inventory(config), expected)
    if not original_running(current, before, expected):
        raise Held('wake refused: original occupant no longer verified')
    if current.get('blockedWorkCount') != 0 or current.get('heldReason'):
        raise Held('wake refused: work blocked or seat held')
    needs = current.get('arbitratedNeedsInput')
    if needs and (not isinstance(needs, dict) or needs.get('count', 1) != 0):
        raise Held('wake refused: seat needs input')
    rows = task_ownership(config, current, expected)
    if current.get('inProgressWorkCount') == 0:
        return {'state': 'not-needed', 'taskConsumption': 'requires-separate-result-verification'}
    claims = owned_work(rows, expected['session'], current.get('inProgressWorkCount'))
    if claims != original_claims:
        raise Held('wake refused: original queue ownership changed')
    native_pid = (current.get('identityVerdict') or {}).get('evidence', {}).get('observedPid')
    if type(native_pid) is not int or native_pid <= 1:
        raise Held('wake refused: exact native occupant process missing')
    identity = json.dumps([expected, before['resumeToken'], native_pid, claims], sort_keys=True)
    attempt_id = hashlib.sha256(identity.encode()).hexdigest()
    path = Path(config['runtime']) / 'run' / f'guardian-wake-{attempt_id}.json'
    ids = [claim[0] for claim in claims]
    prompt = ('OpenRig recovery: resume original owned queue items ' + json.dumps(ids)
              + '; read full original rows and knowledge/CONTINUITY.md; continue only admitted scope, '
              'preserve checkpoint, do not create/reclaim replacement tasks. '
              'Return existing results through native handoff.')
    attempt = {'state': 'attempting', 'attemptId': attempt_id, 'seat': expected['session'],
               'rigId': expected['rigId'], 'nodeId': expected['nodeId'], 'nativePid': native_pid,
               'activeOwnedQitemIds': ids, 'taskConsumption': 'unverified', 'reportAcceptance': 'unverified',
               'resumeTokenSha256': hashlib.sha256(before['resumeToken'].encode()).hexdigest(),
               'promptSha256': hashlib.sha256(prompt.encode()).hexdigest(),
               'observedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    if not exclusive_json(path, attempt):
        prior = json.loads(path.read_text())
        state = prior.get('state')
        if state not in ('native-send-acknowledged', 'native-obligations-terminal', 'unconfirmed-no-retry'):
            state = 'already-attempted'
        return {'state': state, 'attemptId': attempt_id, 'alreadyAttempted': True,
                'receipt': str(path), 'taskConsumption': prior.get('taskConsumption', 'unverified'), 'resend': False}
    # No force/raw/dangerous/prompt-driving flags; native guard must prove idle.
    try:
        result = json_command([config['rig'], 'send', expected['session'], prompt,
                               '--wait-for-idle', '10', '--json'], 80)
        if isinstance(result, dict) and result.get('ok') is True and result.get('sent') is not False and result.get('outcome') != 'retained':
            attempt.update(state='native-send-acknowledged', nativeOutcome=result.get('outcome'))
        else:
            attempt.update(state='unconfirmed-no-retry')
    except (Held, OSError, ValueError, subprocess.SubprocessError):
        attempt.update(state='unconfirmed-no-retry')
    attempt['completedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(path, attempt)
    return {'state': attempt['state'], 'attemptId': attempt_id, 'receipt': str(path),
            'taskConsumption': 'unverified', 'resend': False}


def reconcile_wake_failures(config, expected, current):
    """A new executor PID never erases unresolved work in the same conversation."""
    summaries = []
    for path in sorted((Path(config['runtime'])/'run').glob('guardian-wake-*.json')):
        receipt = json.loads(path.read_text())
        if (receipt.get('seat') != expected['session'] or receipt.get('nodeId') != expected['nodeId']
                or (receipt.get('resumeTokenSha256') and receipt['resumeTokenSha256'] != hashlib.sha256((current.get('resumeToken') or '').encode()).hexdigest())
                or receipt.get('state') not in ('attempting', 'unconfirmed-no-retry')):
            continue
        ids = receipt.get('activeOwnedQitemIds')
        if not isinstance(ids, list) or not ids:
            raise Held('durable wake receipt missing original task identities')
        terminal = {}
        for qitem_id in ids:
            item = json_command([config['rig'], 'queue', 'show', qitem_id, '--full', '--json'])
            if (not isinstance(item, dict) or item.get('qitemId') != qitem_id
                    or item.get('destinationSession') != expected['session']):
                raise Held('durable wake source job identity mismatch')
            if item.get('state') in ('done', 'handed-off', 'canceled', 'failed', 'denied'):
                terminal[qitem_id] = item['state']
        if len(terminal) == len(ids):
            receipt.update(state='native-obligations-terminal', taskConsumption='native-terminal',
                           nativeTerminalStates=terminal, reportAcceptance='unverified',
                           reconciledAt=dt.datetime.now(dt.timezone.utc).isoformat())
            atomic_json(path, receipt)
            summaries.append({'receipt': str(path), 'state': 'native-obligations-terminal',
                              'reportAcceptance': 'unverified'})
        else:
            summaries.append({'receipt': str(path), 'state': 'unconfirmed-no-retry',
                              'taskContinuationUnconfirmed': True, 'reportAcceptance': 'unverified'})
    return summaries


def reconcile_late_launches(config, expected, current):
    """Observe prior apply outcomes without issuing another launch."""
    results = []
    token_hash = hashlib.sha256((current.get('resumeToken') or '').encode()).hexdigest()
    for path in sorted((Path(config['runtime'])/'run').glob('guardian-launch-*.json')):
        receipt = json.loads(path.read_text())
        if (receipt.get('seat') != expected['session'] or receipt.get('nodeId') != expected['nodeId']
                or receipt.get('resumeTokenSha256') != token_hash
                or receipt.get('state') not in ('attempting', 'pending-reconciliation', 'original-running-verified-awaiting-wake')):
            continue
        if not original_running(current, current, expected):
            raise Held('prior launch cannot yet be reconciled')
        claims = [tuple(claim) for claim in receipt.get('originalClaims', [])]
        wake = wake_owned_work(config, expected, current, claims)
        receipt.update(state='original-running-verified',
                       verifiedNativePid=(current.get('identityVerdict') or {}).get('evidence', {}).get('observedPid'),
                       restoredOccupantEpoch=occupant_epoch(current), continuationWake=wake,
                       verifiedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        atomic_json(path, receipt)
        results.append(wake)
    return results


def ensure_caffeinate(config, daemon_pid):
    base = Path(config['runtime'])
    receipt_path = base / 'run' / 'guardian-caffeinate.json'
    command = f'/usr/bin/caffeinate -i -w {daemon_pid}'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('daemonPid') == daemon_pid and process(receipt.get('pid')) == (config['uid'], command):
            return receipt['pid']
    adopt = config.get('adoptCaffeinatePid')
    if type(adopt) is int and process(adopt) == (config['uid'], command):
        pid = adopt
    else:
        child = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(daemon_pid)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        pid = child.pid
        time.sleep(0.05)
        if process(pid) != (config['uid'], command):
            raise Held('sleep assertion identity unconfirmed')
    atomic_json(receipt_path, {'pid': pid, 'daemonPid': daemon_pid, 'command': command})
    return pid


def supervise(config):
    validate_config(config)
    verify_volume(config)
    base = Path(config['runtime'])
    with open(base / 'run' / 'guardian.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': True, 'state': 'already-supervised'}
        health = health_read(config)
        started = False
        if health is None:
            state_path = base / 'daemon.json'
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if process(state.get('pid')) is not None:
                    raise Held('recorded process still exists; listener refused; preserved')
            run([config['rig'], 'daemon', 'start', '--no-kernel', '--host', '127.0.0.1', '--port', '7433'], 90)
            started = True
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                health = health_read(config)
                if health is not None:
                    break
                time.sleep(0.25)
            if health is None:
                raise Held('started daemon did not become ready; no stop or retry')
        validate_health(health, json.loads((base / 'daemon.json').read_text()), config)
        caffeinate_pid = ensure_caffeinate(config, health['pid'])
        outcomes = []
        for seat in config['seats']:
            expected = dict(seat, hostId=config['hostId'])
            row = matching(inventory(config), expected)
            owned_rows = task_ownership(config, row, expected)
            decision, reason = seat_decision(row, expected,
                allow_in_progress=config.get('allowInProgressRecovery'), owned_rows=owned_rows)
            outcome = {'seat': seat['session'], 'decision': decision, 'reason': reason}
            outcomes.append(outcome)
            if decision != 'recover':
                if decision == 'preserve' and reason == 'live':
                    try:
                        outcome['lateLaunchReconciliation'] = reconcile_late_launches(config, expected, row)
                        wake_results = reconcile_wake_failures(config, expected, row)
                        if wake_results:
                            outcome['durableWakeReconciliation'] = wake_results
                        if any(item.get('taskContinuationUnconfirmed') for item in wake_results):
                            outcome['taskContinuationUnconfirmed'] = True
                    except (Held, OSError, ValueError, subprocess.SubprocessError) as error:
                        outcome.update(decision='hold', reason=str(error))
                continue
            try:
                plan = json_command([config['rig'], 'launch', seat['rigId'], seat['logicalId'], '--plan', '--json'])
                resume_plan = resume_preview(seat['rigId'])
                snapshot_id = validate_plan(plan, resume_plan, expected)
                fresh = matching(inventory(config), expected)
                fresh_owned = task_ownership(config, fresh, expected)
                if (seat_decision(fresh, expected, allow_in_progress=config.get('allowInProgressRecovery'), owned_rows=fresh_owned)[0] != 'recover'
                        or fresh.get('resumeToken') != row.get('resumeToken')
                        or (owned_work(fresh_owned, expected['session'], fresh['inProgressWorkCount']) if fresh_owned else [])
                        != (owned_work(owned_rows, expected['session'], row['inProgressWorkCount']) if owned_rows else [])):
                    raise Held('seat changed before recovery; preserved')
                active_ids = [claim[0] for claim in owned_work(fresh_owned, expected['session'], fresh['inProgressWorkCount'])] if fresh_owned else []
                claims = owned_work(fresh_owned, expected['session'], fresh['inProgressWorkCount']) if fresh_owned else []
                attempt_path, attempt, apply_once = launch_attempt(config, expected, row, snapshot_id, claims)
                snapshot_id = attempt['snapshotId']
                ambiguous = not apply_once
                if apply_once:
                    try:
                        result = json_command([config['rig'], 'launch', seat['rigId'], seat['logicalId'], '--snapshot-id', snapshot_id, '--json'], 150)
                        if not isinstance(result, dict) or result.get('ok') is not True or result.get('warnings') or result.get('failedTargets') or result.get('held'):
                            ambiguous = True
                    except (Held, OSError, ValueError, subprocess.SubprocessError):
                        ambiguous = True
                try:
                    restored = reconcile_launch(config, expected, row)
                except Held:
                    attempt.update(state='pending-reconciliation', commandOutcome='ambiguous' if ambiguous else 'acknowledged')
                    atomic_json(attempt_path, attempt)
                    raise
                attempt.update(state='original-running-verified-awaiting-wake' if claims else 'original-running-verified', commandOutcome='ambiguous-reconciled' if ambiguous else 'acknowledged',
                               verifiedNativePid=(restored.get('identityVerdict') or {}).get('evidence', {}).get('observedPid'),
                               restoredOccupantEpoch=occupant_epoch(restored), verifiedAt=dt.datetime.now(dt.timezone.utc).isoformat())
                atomic_json(attempt_path, attempt)
                outcome.update(decision='recovered', reason='same recorded conversation verified', snapshotId=snapshot_id,
                               launchCommandOutcome='ambiguous-reconciled' if ambiguous else 'acknowledged',
                               activeOwnedQitemIds=active_ids)
                outcome['continuationWake'] = wake_owned_work(config, expected, row, claims)
                attempt.update(state='original-running-verified', continuationWake=outcome['continuationWake'])
                atomic_json(attempt_path, attempt)
                if outcome['continuationWake']['state'] in ('unconfirmed-no-retry', 'already-attempted'):
                    outcome['taskContinuationUnconfirmed'] = True
                receipt = dict(outcome, observedAt=dt.datetime.now(dt.timezone.utc).isoformat(),
                               daemonPid=health['pid'], rigId=seat['rigId'], nodeId=seat['nodeId'])
                atomic_json(base / 'run' / f"guardian-recovery-{time.time_ns()}.json", receipt)
            except Held as error:
                outcome.update(decision='hold', reason=str(error))
        return {'ok': all(row['decision'] != 'hold' and not row.get('taskContinuationUnconfirmed') for row in outcomes),
                'daemonPid': health['pid'], 'daemonStarted': started,
                'caffeinatePid': caffeinate_pid, 'seats': outcomes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        result = supervise(config)
    except (Held, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result = {'ok': False, 'state': 'held', 'reason': str(error)}
    result['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
    # Atomic status contains no raw CLI output, credentials or conversation tokens.
    try:
        if (result.get('state') != 'already-supervised' and 'config' in locals()
                and config.get('runtime') and Path(config['runtime']).is_dir()):
            validate_config(config)
            verify_volume(config)
            atomic_json(Path(config['runtime']) / 'run' / 'guardian-status.json', result)
    except (Held, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    print(json.dumps(result))
    return 0 if result['ok'] else 75


if __name__ == '__main__':
    sys.exit(main())
