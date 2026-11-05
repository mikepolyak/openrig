import copy
import datetime as dt
import errno
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import guardian as g

NOW = dt.datetime(2026, 11, 5, 11, 0, tzinfo=dt.timezone.utc)
SEAT = dict(rigId='rig-id', nodeId='node-id', logicalId='audit.user',
            session='audit-user@prankalot-heavy', runtime='codex', hostId='host-remote')


def dead():
    return dict(rigId='rig-id', nodeId='node-id', logicalId='audit.user',
                canonicalSessionName='audit-user@prankalot-heavy', runtime='codex',
                hostSelfId='host-remote', intentionallyStopped=False,
                sessionStatus='detached', terminalActive=False, heldReason=None,
                inProgressWorkCount=0, blockedWorkCount=0,
                resumeType='codex_id', resumeToken='private-session-reference',
                startupCompletedAt='2026-11-05T10:00:00Z',
                identityVerdict={'reason': 'session_missing', 'observedAt': NOW.isoformat(),
                                 'verdict': 'session_missing'})


class Decisions(unittest.TestCase):
    def decide(self, row):
        return g.seat_decision(row, SEAT, now=NOW)[0]

    def test_proved_dead_recovers(self):
        self.assertEqual(self.decide(dead()), 'recover')

    def test_intentional_stop_preserved(self):
        row = dead(); row['intentionallyStopped'] = True
        self.assertEqual(self.decide(row), 'preserve')

    def test_live_idle_not_relaunched(self):
        row = dead(); row.update(sessionStatus='running', terminalActive=False,
                                 identityVerdict={'verdict': 'verified'})
        self.assertEqual(self.decide(row), 'preserve')

    def test_unknown_and_foreign_hold(self):
        for mutation in [dict(sessionStatus='unknown'), dict(intentionallyStopped=None),
                         dict(hostSelfId='foreign'), dict(nodeId='replacement'),
                         dict(terminalActive=None), dict(heldReason='resource-lock'),
                         dict(inProgressWorkCount=1), dict(blockedWorkCount=1),
                         dict(resumeToken=''), dict(resumeType='unknown')]:
            with self.subTest(mutation=mutation):
                row = dead(); row.update(mutation)
                self.assertEqual(self.decide(row), 'hold')

    def test_absence_unknown_stale_and_future_hold(self):
        for verdict in [None, {'reason': 'tmux_unavailable'},
                        {'reason': 'pane_pid_gone', 'observedAt': NOW.isoformat()},
                        {'reason': 'session_missing', 'observedAt': (NOW-dt.timedelta(seconds=91)).isoformat()},
                        {'reason': 'session_missing', 'observedAt': (NOW+dt.timedelta(seconds=10)).isoformat()}]:
            with self.subTest(verdict=verdict):
                row = dead(); row['identityVerdict'] = verdict
                self.assertEqual(self.decide(row), 'hold')

    def test_ambiguous_or_missing_seat_holds(self):
        self.assertEqual(self.decide(None), 'hold')
        self.assertIsNone(g.matching([dead(), dead()], SEAT))

    def test_inflight_requires_opt_in_and_exact_owned_claim(self):
        row = dead(); row['inProgressWorkCount'] = 1
        owned = [{'qitemId': 'job1', 'destinationSession': SEAT['session'], 'state': 'in-progress',
                  'claimedAt': NOW.isoformat(), 'blockedOn': None}]
        self.assertEqual(g.seat_decision(row, SEAT, now=NOW, owned_rows=owned)[0], 'hold')
        self.assertEqual(g.seat_decision(row, SEAT, now=NOW, allow_in_progress=True, owned_rows=owned)[0], 'recover')
        for mutation in [{'destinationSession': 'other@rig'}, {'state': 'blocked'},
                         {'claimedAt': None}, {'blockedOn': 'input'}, {'humanQuestions': ['question']}]:
            with self.subTest(mutation=mutation):
                changed = dict(owned[0], **mutation)
                self.assertEqual(g.seat_decision(row, SEAT, now=NOW, allow_in_progress=True, owned_rows=[changed])[0], 'hold')
        self.assertEqual(g.seat_decision(row, SEAT, now=NOW, allow_in_progress=True, owned_rows=[])[0], 'hold')
        row['blockedWorkCount'] = 1
        self.assertEqual(g.seat_decision(row, SEAT, now=NOW, allow_in_progress=True, owned_rows=owned)[0], 'hold')

    def test_needs_input_not_recovered(self):
        row = dead(); row['arbitratedNeedsInput'] = {'count': 1, 'reason': 'permission'}
        self.assertEqual(self.decide(row), 'hold')


class Plans(unittest.TestCase):
    def setUp(self):
        self.plan = dict(ok=True, planOnly=True,
                         targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                         nonTargetEffects={'mode': 'unchanged', 'affected': []},
                         snapshotSelection={'snapshotId': 'snap'})
        self.resume = dict(status='plan', snapshot={'id': 'snap'},
                           nodes=[{'logicalId': 'audit.user', 'intendedAction': 'resume-original',
                                   'tokenState': 'present', 'freshRequired': False}])

    def test_exact_continuation(self):
        self.assertEqual(g.validate_plan(self.plan, self.resume, SEAT), 'snap')

    def test_forecast_uses_native_readonly_launch_plan_endpoint(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps(self.resume))
        with patch('guardian.urllib.request.urlopen', return_value=response) as opened:
            result = g.resume_preview('rig/id')
            self.assertEqual(result, self.resume)
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:7433/api/rigs/rig%2Fid/launch-plan')
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.data, b'{}')

    def test_nullable_and_malformed_forecast_is_held_without_traceback(self):
        for resume in [None, [], {'status': 'plan', 'snapshot': None},
                       dict(self.resume, snapshot=[]), dict(self.resume, snapshot='snap'),
                       dict(self.resume, nodes=None), dict(self.resume, nodes={}),
                       dict(self.resume, nodes=[None]), dict(self.resume, nodes=['bad'])]:
            with self.subTest(resume=resume):
                with self.assertRaises(g.Held): g.validate_plan(self.plan, resume, SEAT)
        for plan in [None, [], dict(self.plan, snapshotSelection=None),
                     dict(self.plan, snapshotSelection=[]), dict(self.plan, snapshotSelection={'snapshotId': None}),
                     dict(self.plan, nonTargetEffects=None), dict(self.plan, targetNodes=None)]:
            with self.subTest(plan=plan):
                with self.assertRaises(g.Held): g.validate_plan(plan, self.resume, SEAT)

    def test_fresh_stale_missing_and_other_target_refused(self):
        for mutation in [dict(intendedAction='fresh-primed'), dict(intendedAction='awaiting-decision'),
                         dict(tokenState='stale'), dict(tokenState='unverified'), dict(freshRequired=True)]:
            with self.subTest(mutation=mutation):
                resume = copy.deepcopy(self.resume); resume['nodes'][0].update(mutation)
                with self.assertRaises(g.Held): g.validate_plan(self.plan, resume, SEAT)
        for mutation in [dict(targetNodes=[]), dict(targetNodes=[{'nodeId': 'foreign', 'logicalId': 'audit.user'}]),
                         dict(nonTargetEffects={'mode': 'detach_and_hold', 'affected': []}),
                         dict(nonTargetEffects={'mode': 'unchanged', 'affected': ['sibling']}),
                         dict(snapshotSelection={'snapshotId': 'changed'})]:
            with self.subTest(mutation=mutation):
                plan = dict(self.plan, **mutation)
                with self.assertRaises(g.Held): g.validate_plan(plan, self.resume, SEAT)


class ContinuationWake(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        (Path(self.temp.name) / 'run').mkdir()
        self.config = {'runtime': self.temp.name, 'rig': '/rig', 'allowInProgressRecovery': True}
        self.before = dead()
        self.after = dict(self.before, inProgressWorkCount=1, sessionStatus='running',
                          identityVerdict={'verdict': 'verified', 'evidence': {'observedPid': 303}})
        self.owned = [{'qitemId': 'job1', 'destinationSession': SEAT['session'], 'state': 'in-progress',
                       'claimedAt': NOW.isoformat(), 'blockedOn': None}]
        self.claims = [('job1', NOW.isoformat())]

    def tearDown(self): self.temp.cleanup()

    def test_before_send_receipt_once_and_consumption_unverified(self):
        def acknowledge(argv, timeout):
            receipt = json.loads(next((Path(self.temp.name)/'run').glob('guardian-wake-*.json')).read_text())
            self.assertEqual(receipt['state'], 'attempting')
            self.assertEqual(receipt['taskConsumption'], 'unverified')
            self.assertEqual(argv[1:3], ['send', SEAT['session']])
            self.assertEqual(argv[-3:], ['--wait-for-idle', '10', '--json'])
            self.assertNotIn('--force', argv)
            self.assertNotIn('--dangerously-interact', argv)
            return {'ok': True, 'sent': True}
        with patch.object(g, 'inventory', return_value=[self.after]), \
             patch.object(g, 'task_ownership', return_value=self.owned), \
             patch.object(g, 'json_command', side_effect=acknowledge) as command:
            first = g.wake_owned_work(self.config, SEAT, self.before, self.claims)
            second = g.wake_owned_work(self.config, SEAT, self.before, self.claims)
            self.assertEqual(first['state'], 'native-send-acknowledged')
            self.assertEqual(first['taskConsumption'], 'unverified')
            self.assertEqual(second['state'], 'native-send-acknowledged')
            self.assertTrue(second['alreadyAttempted'])
            self.assertEqual(command.call_count, 1)

    def test_ambiguous_send_never_retried(self):
        with patch.object(g, 'inventory', return_value=[self.after]), \
             patch.object(g, 'task_ownership', return_value=self.owned), \
             patch.object(g, 'json_command', side_effect=g.Held('timed out')) as command:
            first = g.wake_owned_work(self.config, SEAT, self.before, self.claims)
            second = g.wake_owned_work(self.config, SEAT, self.before, self.claims)
            self.assertEqual(first['state'], 'unconfirmed-no-retry')
            self.assertEqual(second['state'], 'unconfirmed-no-retry')
            self.assertTrue(second['alreadyAttempted'])
            self.assertEqual(command.call_count, 1)

    def test_no_send_for_blocked_unverified_wrong_occupant_or_owner(self):
        for mutation in [dict(blockedWorkCount=1), dict(identityVerdict={'verdict': 'unknown'}),
                         dict(resumeToken='different'), dict(nodeId='other'),
                         dict(arbitratedNeedsInput={'count': 1})]:
            with self.subTest(mutation=mutation):
                row = dict(self.after, **mutation)
                with patch.object(g, 'inventory', return_value=[row]), \
                     patch.object(g, 'json_command') as command:
                    with self.assertRaises(g.Held): g.wake_owned_work(self.config, SEAT, self.before, self.claims)
                    command.assert_not_called()
        wrong_owner = [dict(self.owned[0], destinationSession='other@rig')]
        with patch.object(g, 'inventory', return_value=[self.after]), \
             patch.object(g, 'task_ownership', return_value=wrong_owner), patch.object(g, 'json_command') as command:
            with self.assertRaises(g.Held): g.wake_owned_work(self.config, SEAT, self.before, self.claims)
            command.assert_not_called()


class DaemonSafety(unittest.TestCase):
    def setUp(self):
        self.config = dict(uid=501, runtime='/Volumes/D/runtime', hostId='host',
                           nodeBinary='/node', daemonScript='/source/daemon.js')
        self.health = dict(status='ok', pid=55, euid=501, selfHostId='host', bind={'hosts': ['127.0.0.1']})
        self.state = dict(pid=55, db='/Volumes/D/runtime/openrig.sqlite')

    def test_exact_identity(self):
        g.validate_health(self.health, self.state, self.config, lambda pid: (501, '/node /source/daemon.js'))

    def test_foreign_pid_uid_db_listener_refused(self):
        for changed in [dict(pid=56), dict(euid=502), dict(selfHostId='foreign'),
                        dict(bind={'hosts': ['0.0.0.0']}), dict(status='starting')]:
            with self.subTest(changed=changed):
                with self.assertRaises(g.Held):
                    g.validate_health(dict(self.health, **changed), self.state, self.config,
                                      lambda pid: (501, '/node /source/daemon.js'))
        with self.assertRaises(g.Held):
            g.validate_health(self.health, self.state, self.config, lambda pid: (501, '/other'))

    def test_refusal_only_start_signal(self):
        for error in [urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'refused')),
                      urllib.error.URLError(TimeoutError('timeout')),
                      urllib.error.URLError(PermissionError('denied'))]:
            with self.subTest(error=error):
                with patch('guardian.urllib.request.urlopen', side_effect=error):
                    if isinstance(error.reason, ConnectionRefusedError):
                        self.assertIsNone(g.health_read(self.config))
                    else:
                        with self.assertRaises(g.Held): g.health_read(self.config)

    def test_config_refuses_user_and_empty_allowlist(self):
        with self.assertRaises(g.Held): g.validate_config({})


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / 'runtime'
        (self.base / 'run').mkdir(parents=True)
        (self.base / 'daemon.json').write_text(json.dumps({'pid': 55, 'db': str(self.base / 'openrig.sqlite')}))
        self.config = dict(uid=os.getuid(), runtime=str(self.base), volume=self.temp.name,
                           volumeUuid='uuid', hostId='host-remote', rig='/rig', nodeBinary='/node',
                           daemonScript='/source/daemon.js', seats=[{k: v for k, v in SEAT.items() if k != 'hostId'}])
        self.health = dict(status='ok', pid=55, euid=os.getuid(), selfHostId='host-remote', bind={'hosts': ['127.0.0.1']})
        self.process = (os.getuid(), '/node /source/daemon.js')

    def tearDown(self): self.temp.cleanup()

    def test_live_no_launch_no_queue(self):
        row = dead(); row.update(sessionStatus='running', identityVerdict={'verdict': 'verified'})
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', return_value=[row]), patch.object(g, 'run') as run, \
             patch.object(g, 'json_command') as command:
            result = g.supervise(self.config)
            self.assertTrue(result['ok'])
            self.assertEqual(result['seats'][0]['decision'], 'preserve')
            run.assert_not_called(); command.assert_not_called()

    def test_foreign_listener_never_started_or_replaced(self):
        foreign = dict(self.health, selfHostId='foreign')
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=foreign), \
             patch.object(g, 'run') as run:
            with self.assertRaises(g.Held): g.supervise(self.config)
            run.assert_not_called()

    def test_unreachable_unknown_never_starts(self):
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', side_effect=g.Held('timeout')), \
             patch.object(g, 'run') as run:
            with self.assertRaises(g.Held): g.supervise(self.config)
            run.assert_not_called()

    def test_refused_listener_with_existing_process_preserved(self):
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=None), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'run') as run:
            with self.assertRaises(g.Held): g.supervise(self.config)
            run.assert_not_called()

    def test_exact_single_recovery_pins_snapshot(self):
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        after = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified'})
        plan = dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                    nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': 'snap'})
        resume = dict(status='plan', snapshot={'id': 'snap'}, nodes=[dict(logicalId='audit.user',
                      intendedAction='resume-original', tokenState='present', freshRequired=False)])
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', side_effect=[[before], [before], [after]]), \
             patch.object(g, 'resume_preview', return_value=resume), \
             patch.object(g, 'json_command', side_effect=[plan, {'ok': True}]) as command:
            result = g.supervise(self.config)
            self.assertTrue(result['ok'])
            self.assertEqual(result['seats'][0]['decision'], 'recovered')
            self.assertEqual(command.call_args_list[-1].args[0],
                             ['/rig', 'launch', 'rig-id', 'audit.user', '--snapshot-id', 'snap', '--json'])
            self.assertEqual(len(list((self.base / 'run').glob('guardian-recovery-*.json'))), 1)

    def test_inflight_recovery_records_original_job_without_queue_mutation(self):
        self.config['allowInProgressRecovery'] = True
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        before['inProgressWorkCount'] = 1
        after = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified', 'evidence': {'observedPid': 303}})
        plan = dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                    nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': 'snap'})
        resume = dict(status='plan', snapshot={'id': 'snap'}, nodes=[dict(logicalId='audit.user',
                      intendedAction='resume-original', tokenState='present', freshRequired=False)])
        owned = [{'qitemId': 'original-job', 'destinationSession': SEAT['session'],
                  'state': 'in-progress', 'claimedAt': NOW.isoformat(), 'blockedOn': None}]
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', side_effect=[[before], [before], [after], [after]]), \
             patch.object(g, 'resume_preview', return_value=resume), \
             patch.object(g, 'json_command', side_effect=[owned, plan, owned, {'ok': True}, owned, {'ok': True, 'sent': True}]) as command:
            result = g.supervise(self.config)
            self.assertTrue(result['ok'])
            self.assertEqual(result['seats'][0]['activeOwnedQitemIds'], ['original-job'])
            receipt = json.loads(next((self.base/'run').glob('guardian-recovery-*.json')).read_text())
            self.assertEqual(receipt['activeOwnedQitemIds'], ['original-job'])
            queue_calls = [call.args[0] for call in command.call_args_list if call.args[0][1] == 'queue']
            self.assertEqual(len(queue_calls), 3)
            self.assertTrue(all(call[2] == 'list' for call in queue_calls))
            sends = [call.args[0] for call in command.call_args_list if call.args[0][1] == 'send']
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0][-3:], ['--wait-for-idle', '10', '--json'])
            self.assertEqual(result['seats'][0]['continuationWake']['taskConsumption'], 'unverified')
        # A later periodic pass preserves this live occupant and never sends again.
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', return_value=[after]), \
             patch.object(g, 'json_command', return_value=owned) as command:
            next_result = g.supervise(self.config)
            self.assertEqual(next_result['seats'][0]['decision'], 'preserve')
            self.assertTrue(all(call.args[0][1] != 'send' for call in command.call_args_list))

    def test_ambiguous_apply_reconciles_once_without_retry(self):
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        after = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified'})
        plan = dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                    nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': 'snap'})
        resume = dict(status='plan', snapshot={'id': 'snap'}, nodes=[dict(logicalId='audit.user',
                      intendedAction='resume-original', tokenState='present', freshRequired=False)])
        for apply_response in [g.Held('CLI timeout'), g.Held('invalid JSON'), ['malformed'], {'ok': False}]:
            with self.subTest(response=apply_response):
                for receipt in (self.base/'run').glob('guardian-launch-*.json'):
                    receipt.unlink()  # independent fixture for each outcome case
                with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
                     patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
                     patch.object(g, 'inventory', side_effect=[[before], [before], [before], [after]]), \
                     patch.object(g, 'resume_preview', return_value=resume), patch.object(g.time, 'sleep'), \
                     patch.object(g, 'json_command', side_effect=[plan, apply_response]) as command:
                    result = g.supervise(self.config)
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['seats'][0]['launchCommandOutcome'], 'ambiguous-reconciled')
                    self.assertEqual(command.call_count, 2)  # one preview, exactly one apply

    def test_ambiguous_apply_other_occupant_holds_without_retry(self):
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        wrong = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified'}, resumeToken='replacement')
        plan = dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                    nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': 'snap'})
        resume = dict(status='plan', snapshot={'id': 'snap'}, nodes=[dict(logicalId='audit.user',
                      intendedAction='resume-original', tokenState='present', freshRequired=False)])
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', side_effect=[[before], [before], [wrong]]), \
             patch.object(g, 'resume_preview', return_value=resume), \
             patch.object(g.time, 'monotonic', side_effect=[0, 21]), \
             patch.object(g, 'json_command', side_effect=[plan, g.Held('timeout')]) as command:
            result = g.supervise(self.config)
            self.assertFalse(result['ok'])
            self.assertEqual(result['seats'][0]['decision'], 'hold')
            self.assertEqual(command.call_count, 2)

    def test_two_passes_dead_ambiguous_apply_only_once_despite_new_snapshot(self):
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        def plan(snapshot):
            return dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                        nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': snapshot})
        def resume(snapshot):
            return dict(status='plan', snapshot={'id': snapshot}, nodes=[dict(logicalId='audit.user',
                        intendedAction='resume-original', tokenState='present', freshRequired=False)])
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', return_value=[before]), \
             patch.object(g, 'resume_preview', side_effect=[resume('first'), resume('newer')]), \
             patch.object(g.time, 'monotonic', side_effect=[0, 21, 0, 21]), \
             patch.object(g, 'json_command', side_effect=[plan('first'), g.Held('ambiguous timeout'), plan('newer')]) as command:
            first = g.supervise(self.config)
            second = g.supervise(self.config)
            self.assertFalse(first['ok']); self.assertFalse(second['ok'])
            applies = [call.args[0] for call in command.call_args_list if '--plan' not in call.args[0]]
            self.assertEqual(len(applies), 1)
            receipt = json.loads(next((self.base/'run').glob('guardian-launch-*.json')).read_text())
            self.assertEqual(receipt['state'], 'pending-reconciliation')
            self.assertEqual(receipt['snapshotId'], 'first')

    def test_ambiguous_wake_stays_degraded_until_original_native_terminal(self):
        self.config['allowInProgressRecovery'] = True
        before = dead(); before['identityVerdict']['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        before['inProgressWorkCount'] = 1
        after = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified', 'evidence': {'observedPid': 303}})
        next_executor = dict(after, startupCompletedAt='2026-11-05T11:30:00Z',
                             identityVerdict={'verdict': 'verified', 'evidence': {'observedPid': 304}})
        finished = dict(next_executor, inProgressWorkCount=0)
        plan = dict(ok=True, planOnly=True, targetNodes=[{'nodeId': 'node-id', 'logicalId': 'audit.user'}],
                    nonTargetEffects={'mode': 'unchanged', 'affected': []}, snapshotSelection={'snapshotId': 'snap'})
        resume = dict(status='plan', snapshot={'id': 'snap'}, nodes=[dict(logicalId='audit.user',
                      intendedAction='resume-original', tokenState='present', freshRequired=False)])
        item = {'qitemId': 'original-job', 'destinationSession': SEAT['session'], 'state': 'in-progress',
                'claimedAt': NOW.isoformat(), 'blockedOn': None}
        owned = [item]
        terminal = dict(item, state='handed-off')
        replies = [owned, plan, owned, {'ok': True}, owned, g.Held('send unknown'), owned, item, terminal]
        with patch.object(g, 'verify_volume'), patch.object(g, 'health_read', return_value=self.health), \
             patch.object(g, 'process', return_value=self.process), patch.object(g, 'ensure_caffeinate', return_value=66), \
             patch.object(g, 'inventory', side_effect=[[before], [before], [after], [after], [next_executor], [finished]]), \
             patch.object(g, 'resume_preview', return_value=resume), \
             patch.object(g, 'json_command', side_effect=replies) as command:
            first = g.supervise(self.config)
            second = g.supervise(self.config)
            third = g.supervise(self.config)
            self.assertFalse(first['ok']); self.assertFalse(second['ok']); self.assertTrue(third['ok'])
            self.assertTrue(second['seats'][0]['taskContinuationUnconfirmed'])
            self.assertEqual(len([call for call in command.call_args_list if call.args[0][1] == 'send']), 1)
            receipt = json.loads(next((self.base/'run').glob('guardian-wake-*.json')).read_text())
            self.assertEqual(receipt['state'], 'native-obligations-terminal')
            self.assertEqual(receipt['nativeTerminalStates'], {'original-job': 'handed-off'})
            self.assertEqual(receipt['reportAcceptance'], 'unverified')

    def test_unconfirmed_wake_survives_executor_pid_change_same_conversation(self):
        before = dead()
        live = dict(before, sessionStatus='running', identityVerdict={'verdict': 'verified', 'evidence': {'observedPid': 304}})
        import hashlib
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                path = self.base/'run'/f'guardian-wake-pid-change-{legacy}.json'
                receipt = {'state': 'unconfirmed-no-retry', 'seat': SEAT['session'],
                           'rigId': SEAT['rigId'], 'nodeId': SEAT['nodeId'], 'nativePid': 303,
                           'activeOwnedQitemIds': ['original-job'], 'taskConsumption': 'unverified'}
                if not legacy:
                    receipt['resumeTokenSha256'] = hashlib.sha256(before['resumeToken'].encode()).hexdigest()
                path.write_text(json.dumps(receipt))
                item = {'qitemId': 'original-job', 'destinationSession': SEAT['session'], 'state': 'in-progress'}
                with patch.object(g, 'json_command', side_effect=[item, dict(item, state='handed-off')]) as command:
                    pending = g.reconcile_wake_failures(self.config, SEAT, live)
                    terminal = g.reconcile_wake_failures(self.config, SEAT, live)
                    self.assertTrue(pending[0]['taskContinuationUnconfirmed'])
                    self.assertEqual(terminal[0]['state'], 'native-obligations-terminal')
                    self.assertEqual(json.loads(path.read_text())['reportAcceptance'], 'unverified')
                    self.assertTrue(all(call.args[0][1:3] == ['queue', 'show'] for call in command.call_args_list))
                path.unlink()

    def test_new_verified_executor_epoch_can_recover_new_crash(self):
        expected = dict(SEAT)
        before = dead()
        first_path, first_receipt, first = g.launch_attempt(self.config, expected, before, 'snapshot1', [])
        self.assertTrue(first)
        second_path, second_receipt, second = g.launch_attempt(self.config, expected, before, 'snapshot2', [])
        self.assertFalse(second); self.assertEqual(first_path, second_path)
        next_occupant = dict(before, startupCompletedAt='2026-11-05T11:00:00Z')
        with self.assertRaises(g.Held):
            g.launch_attempt(self.config, expected, next_occupant, 'snapshot3', [])
        first_receipt.update(state='original-running-verified', verifiedNativePid=303)
        g.atomic_json(first_path, first_receipt)
        next_path, next_receipt, created = g.launch_attempt(self.config, expected, next_occupant, 'snapshot3', [])
        self.assertTrue(created); self.assertNotEqual(first_path, next_path)


if __name__ == '__main__': unittest.main()
