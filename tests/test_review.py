import argparse
import contextlib
import copy
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goldfish_review as kit
import native_client as nc
import package_kit

FIXTURE = Path(__file__).resolve().parent / 'fixtures/goldfish_fixture.sql'

# A protocol fixture, not a model-quality test. It exercises the complete invocation
# and output validation, with known semantic groupings annotated from the real flags.
FAKE_CLAUDE = r'''
import json, sys
args = sys.argv[1:]
assert args[args.index('--tools')+1] == ''
assert '--strict-mcp-config' in args
servers = json.loads(args[args.index('--mcp-config')+1])['mcpServers']
payload = json.load(sys.stdin)
assert set(servers) == ({'native'} if payload['native_search'] else set())
destination = None
if servers:
    # Simulate the read-only reader: Claude listed collections and saw 'other'.
    assert args[args.index('--allowedTools')+1].split(',')[0] == 'mcp__native__search_native'
    log = servers['native']['args'][servers['native']['args'].index('--log')+1]
    with open(log, 'a') as f:
        f.write(json.dumps({'tool': 'search_native', 'arguments': {'query': 'Docs'},
                            'seen': [{'id': 'other', 'type': 'Collection', 'name': 'Other'}]}) + '\n')
    destination = 'other'
rows = [r for g in payload['flags'] for r in g['flags']]
ids = {r['flag_id'] for r in rows}
items = []
for group, where in (([10,13,15], destination), ([12,14], None)):
    present = sorted(ids.intersection(group))
    if present:
        items.append({'flag_ids': present, 'recommendation': 'include', 'reason': 'One repeated work event.',
            'description': 'Grouped work event', 'action': 'create', 'target_id': None, 'destination_id': where,
            'name': 'Review work event', 'text': 'A single proposed work contribution.',
            'basis': 'captured', 'auto': payload['auto_write'] and group == [12,14],
            'issue_key': 'work-event' if group == [12,14] else '', 'links': []})
        ids.difference_update(present)
for i in sorted(ids):
    family = i == 1002
    items.append({'flag_ids':[i], 'recommendation':'exclude' if family else 'uncertain',
        'reason':'Personal family matter.' if family else 'Needs your judgement.',
        'description':'Family conversation' if family else 'Flag '+str(i),
        'action':'none', 'target_id':None, 'destination_id':None, 'name':'', 'text':'',
        'basis':'inferred', 'auto':False, 'issue_key':'', 'links':[]})
print(json.dumps({'structured_output': {'items': items}}))
'''


class FakeNative:
    records = {}
    writes = []
    keys = {}
    fail_after_commit = False
    starts = 0

    def __init__(self, config):
        pass

    def start(self, intent, writing=False):
        FakeNative.starts += 1
        assert 'Call Mum' not in intent

    def close(self):
        pass

    def record(self, record_id):
        return copy.deepcopy(self.records.get(record_id))

    def call(self, executor, operation, args):
        if executor == 'records_lifecycle':
            self.writes.append((operation, copy.deepcopy(args)))
            return {'ok': True}
        if executor == 'sql_read':
            return {'rows': [{'id': 'target', 'type': 'WorkItem', 'name': 'Existing task', 'summary': 'Work'}]}
        assert executor == 'records_write'
        if operation == 'create_record' and args['idempotency_key'] in self.keys:
            return {'id': self.keys[args['idempotency_key']], 'write_receipt': {'event': {'seq': 1}}}
        self.writes.append((operation, copy.deepcopy(args)))
        if operation == 'manage_links.add':
            return {'ok': True}
        if operation == 'create_record':
            assert 'id' not in args  # KernelV2 refuses supplied ids
            key = args['idempotency_key']
            if key in self.keys:  # Native's idempotent replay returns the record already created
                return {'id': self.keys[key], 'write_receipt': {'event': {'seq': 1}}}
            rid = self.keys[key] = 'rec-%d' % len(self.keys)
        else:
            rid = args['id']
        if operation == 'create_record':
            body = args['body']
        else:
            assert self.records[rid]['body_digest'] == args['if_body_digest']
            body = self.records[rid]['body'] + args['body_append']
        self.records[rid] = {'id': rid, 'body': body, 'body_digest': nc.sha256(body), 'type': args.get('type', 'WorkItem')}
        if self.fail_after_commit:
            raise nc.SyncError('Simulated connection lost after commit')
        return {'id': rid, 'body_digest': nc.sha256(body), 'write_receipt': {'event': {'seq': 1}}}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'state'
        self.db = self.root / 'Goldfish # test.db'
        with sqlite3.connect(self.db) as db:
            db.executescript(FIXTURE.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'install', '--database', str(self.db),
                      '--skills-dir', str(self.root / 'skills')])
        self.config = kit.configuration(self.home)
        self.config['review']['notify'] = False
        self.config['review']['auto_write'] = False  # tests opt in to automatic writes explicitly
        executable = self.root / 'fake_claude.py'
        executable.write_text(FAKE_CLAUDE)
        self.config['review']['claude_command'] = [sys.executable, str(executable)]
        kit.write(self.home / 'config.json', self.config)
        FakeNative.records = {'collection': {'id': 'collection', 'type': 'Collection'},
                              'other': {'id': 'other', 'type': 'Collection', 'name': 'Other'},
                              'target': {'id': 'target', 'type': 'WorkItem', 'body': 'Original',
                                         'body_digest': nc.sha256('Original')}}
        FakeNative.writes = []
        FakeNative.keys = {}
        FakeNative.fail_after_commit = False
        FakeNative.starts = 0
        self.native = patch.object(kit, 'Native', FakeNative)
        self.native.start()

    def tearDown(self):
        self.native.stop()
        self.tmp.cleanup()

    def prepare(self, configured=True):
        if configured:
            self.config.update(native_mcp_url='http://fake-native.invalid/mcp', collection_id='collection')
        kit.write(self.home / 'config.json', self.config)
        with contextlib.redirect_stdout(io.StringIO()):
            kit.review(self.home, self.config)
        return kit.read_plan(Path((self.home / 'latest.txt').read_text().strip()))

    def approve(self, plan, numbers):
        with contextlib.redirect_stdout(io.StringIO()):
            kit.apply(self.home, self.config, plan, numbers, plan['digest'])

    def test_readonly_fixture_lanes_and_no_pending_copies(self):
        before = self.db.read_bytes()
        rows, counts = kit.flags(self.config, kit.state_load(self.home))
        by_id = {r['flag_id']: r for r in rows}
        self.assertEqual(len(rows), 34)
        self.assertEqual(by_id[1001]['lane'], 'careful')
        self.assertEqual(by_id[1002]['lane'], 'careful')
        self.assertEqual(by_id[1]['lane'], 'careful')
        self.assertEqual(by_id[10]['lane'], 'work')
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse((self.home / 'state.json').exists())
        self.assertEqual(counts['hidden_lane_count'], 0)
        with self.assertRaises(sqlite3.OperationalError):
            config = dict(self.config, database=str(self.root / 'absent.db'))
            kit.flags(config, kit.state_load(self.home))
        self.assertFalse((self.root / 'absent.db').exists())

    def test_careful_precedence_unknown_and_visibility(self):
        row = {'app': 'WhatsApp', 'domain': 'github.com', 'snapshot_id': 'x'}
        self.assertEqual(kit.lane(row, self.config), 'careful')
        row.update(app='Chrome', domain='github.com.evil.example')
        self.assertEqual(kit.lane(row, self.config), 'careful')
        self.config['lanes']['agent_visible'] = ['work']
        rows, counts = kit.flags(self.config, kit.state_load(self.home))
        self.assertGreater(counts['hidden_lane_count'], 0)
        self.assertNotIn('Call Mum', json.dumps(kit.groups(rows)))
        self.assertNotIn(1001, [r['flag_id'] for r in rows])

    def test_review_semantic_protocol_and_no_native_writes(self):
        before = self.db.read_bytes()
        plan = self.prepare()
        self.assertIn([10,13,15], [i['flag_ids'] for i in plan['items']])
        self.assertIn([12,14], [i['flag_ids'] for i in plan['items']])
        family = next(i for i in plan['items'] if i['flag_ids'] == [1002])
        self.assertEqual(family['recommendation'], 'exclude')
        self.assertEqual(FakeNative.writes, [])
        state = kit.state_load(self.home)
        self.assertEqual(state['decisions'], {})
        self.assertNotIn('Call Mum', (self.home / 'state.json').read_text())
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(kit.flags(self.config, state, new_only=True)[0], [])
        self.assertEqual(len(kit.flags(self.config, state)[0]), 34) # reviewed != dismissed
        self.assertEqual((self.home / 'reviews' / (plan['id']+'.json')).stat().st_mode & 0o777, 0o600)

    def test_failed_model_or_omitted_flag_preserves_checkpoint(self):
        self.config['review']['claude_command'] = [sys.executable, '-c', 'print("{}")']
        with self.assertRaises(kit.Error):
            kit.review(self.home, self.config, new_only=True)
        self.assertFalse((self.home / 'state.json').exists())
        rows, _ = kit.flags(self.config, kit.state_load(self.home))
        with self.assertRaisesRegex(kit.Error, 'omitted'):
            kit.validate_items([], rows, [])

    def test_approval_required_selection_and_idempotency(self):
        plan = self.prepare()
        item = next(i for i in plan['items'] if i['flag_ids'] == [12,14])
        with self.assertRaises(kit.Error):
            kit.apply(self.home, self.config, plan, [item['number']], 'wrong')
        excluded = next(i for i in plan['items'] if i['recommendation'] == 'exclude')
        with self.assertRaises(kit.Error):
            self.approve(plan, [excluded['number']])
        self.assertEqual(FakeNative.writes, [])
        self.approve(plan, [item['number']])
        self.approve(plan, [item['number']])
        self.assertEqual(len(FakeNative.writes), 1)
        args = FakeNative.writes[0][1]
        self.assertIn('Source: Goldfish', args['body'])
        self.assertIn('Flag 12 | ARTIFACT', args['body'])
        self.assertIn('+00:00', args['body'])
        self.assertNotIn('Call Mum', args['body'])
        self.assertEqual(args['home_id'], 'collection')
        self.assertEqual(kit.state_load(self.home)['decisions']['12']['status'], 'sent')
        # Moving an app between lanes must not make already-sent flags new again.
        self.config['lanes']['careful_apps'].append('native desktop')
        pending, _ = kit.flags(self.config, kit.state_load(self.home))
        self.assertNotIn(12, [r['flag_id'] for r in pending])

    def test_source_change_blocks_and_changed_config_blocks(self):
        plan = self.prepare()
        item = plan['items'][0]
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE salient_flags SET text=? WHERE flag_id=10', ('Changed meaning',))
        with self.assertRaisesRegex(kit.Error, 'Source flag changed'):
            self.approve(plan, [item['number']])
        self.config['collection_id'] = 'different'
        with self.assertRaisesRegex(kit.Error, 'destination changed'):
            self.approve(plan, [item['number']])
        self.assertEqual(FakeNative.writes, [])

    def test_update_guard_and_append_and_comment(self):
        plan = self.prepare()
        item = plan['items'][0]
        item.update(action='update', target_id='target', destination_id=None, if_body_digest=nc.sha256('Original'))
        plan['digest'] = kit.digest({k:v for k,v in plan.items() if k != 'digest'})
        FakeNative.records['target']['body_digest'] = nc.sha256('Human edit')
        with self.assertRaisesRegex(kit.Error, 'edited since review'):
            self.approve(plan, [item['number']])
        self.assertEqual(FakeNative.writes, [])
        FakeNative.records['target']['body_digest'] = nc.sha256('Original')
        self.approve(plan, [item['number']])
        self.assertTrue(FakeNative.records['target']['body'].startswith('Original\n\n'))
        item2 = plan['items'][1]
        item2.update(action='comment', target_id='target', destination_id=None,
                     links=[{'target_id': 'target', 'note': 'Same bug.'}])
        plan['digest'] = kit.digest({k:v for k,v in plan.items() if k != 'digest'})
        self.approve(plan, [item2['number']])
        (create, args), (link_op, link) = FakeNative.writes[-2:]
        self.assertEqual((create, args['type']), ('create_record', 'Annotation'))
        # Native refuses a comment created with links beyond its part_of bearer, so related links follow separately
        self.assertEqual(args['links'], [{'target_id':'target', 'relationship':'part_of'}])
        comment_id = FakeNative.keys[args['idempotency_key']]
        self.assertEqual((link_op, link['source_id'], link['relationship']), ('manage_links.add', comment_id, 'relates_to'))

    def test_destination_must_be_a_seen_live_collection(self):
        plan = self.prepare()
        grouped = next(i for i in plan['items'] if i['flag_ids'] == [10,13,15])
        self.assertEqual((grouped['destination_id'], grouped['destination_name']), ('other', 'Other'))
        self.assertEqual(plan['native_reads'], [{'tool': 'search_native', 'arguments': {'query': 'Docs'}, 'results': 1}])
        self.approve(plan, [grouped['number']])
        self.assertEqual(FakeNative.writes[-1][1]['home_id'], 'other')
        rows, _ = kit.flags(self.config, kit.state_load(self.home))
        unseen = dict(grouped, destination_id='nowhere', flag_ids=[1001])
        with self.assertRaisesRegex(kit.Error, 'collection Claude actually saw'):
            kit.validate_items([{k: unseen[k] for k in kit.ITEM_SCHEMA['required']}],
                               [{'flag_id': 1001}], [], {'collection'})
        bad = dict(unseen, action='comment', target_id='target', destination_id='other')
        with self.assertRaisesRegex(kit.Error, 'Only a new task'):
            kit.validate_items([{k: bad[k] for k in kit.ITEM_SCHEMA['required']}],
                               [{'flag_id': 1001}], [{'id': 'target'}], {'other'})

    def test_search_can_be_turned_off(self):
        self.config['review']['native_search'] = False
        plan = self.prepare()
        self.assertEqual(plan['native_reads'], [])
        self.assertTrue(all(i['destination_id'] is None for i in plan['items']))

    def test_record_by_default_labels_basis_and_can_be_undone(self):
        self.config['review']['auto_write'] = False
        plan = self.prepare()
        self.assertEqual(FakeNative.writes, [])  # off: nothing written without approval
        self.config['review']['auto_write'] = True
        with sqlite3.connect(self.db) as db:  # new flags so a second review has something to do
            db.execute('UPDATE salient_flags SET text=text||? WHERE flag_id IN (12,14)', (' (again)',))
        plan = self.prepare()
        self.assertEqual(len(FakeNative.writes), 1)
        operation, args = FakeNative.writes[0]
        self.assertEqual(args['facets'], {'source': 'goldfish', 'goldfish_basis': 'captured',
                                          'goldfish_written': 'automatic', 'goldfish_issue': 'work-event'})
        self.assertIn('written automatically by Goldfish review, not checked by a person', args['body'])
        self.assertIn('Basis: captured', args['body'])
        self.assertIn('Record-by-default', args['reason'])
        # Not marked auto: still needs approval, and approval is labelled as such.
        other = next(i for i in plan['items'] if i['flag_ids'] == [10,13,15])
        with self.assertRaisesRegex(kit.Error, 'marked for automatic'):
            kit.apply(self.home, self.config, plan, [other['number']], None, automatic=True)
        self.approve(plan, [other['number']])
        self.assertEqual(FakeNative.writes[-1][1]['facets']['goldfish_written'], 'approved')
        # Undo archives the automatic record and keeps its flags out of later reviews.
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'undo', FakeNative.keys[args['idempotency_key']]])
        self.assertEqual(FakeNative.writes[-1], ('archive_record', FakeNative.writes[-1][1]))
        self.assertEqual(kit.state_load(self.home)['decisions']['12']['status'], 'dismissed')
        auto_update = dict(other, action='update', target_id='target', destination_id=None, auto=True)
        with self.assertRaisesRegex(kit.Error, 'Only a new task or comment'):
            kit.validate_items([{k: auto_update[k] for k in kit.ITEM_SCHEMA['required']}],
                               [{'flag_id': f} for f in other['flag_ids']], [{'id': 'target'}], set())

    def test_friction_records_links_and_evidence(self):
        plan = self.prepare()
        item = next(i for i in plan['items'] if i['flag_ids'] == [12,14])
        item['links'] = [{'target_id': 'target', 'note': 'Probably the same bug.'}]
        plan['digest'] = kit.digest({k:v for k,v in plan.items() if k != 'digest'})
        self.approve(plan, [item['number']])
        args = FakeNative.writes[-1][1]
        self.assertEqual(args['facets']['goldfish_issue'], 'work-event')
        self.assertIn({'target_id': 'target', 'relationship': 'relates_to', 'note': 'Probably the same bug.'}, args['links'])
        created = FakeNative.keys[args['idempotency_key']]
        # A repeat sighting appends evidence to the kit's friction record and links separately.
        other = next(i for i in plan['items'] if i['flag_ids'] == [10,13,15])
        other.update(action='evidence', target_id=created, destination_id=None,
                     if_body_digest=FakeNative.records[created]['body_digest'],
                     links=[{'target_id': 'target', 'note': 'Same again.'}])
        plan['digest'] = kit.digest({k:v for k,v in plan.items() if k != 'digest'})
        self.approve(plan, [other['number']])
        self.assertEqual(FakeNative.writes[-2][0], 'update_record')
        self.assertIn('Source: Goldfish', FakeNative.records[created]['body'].split('\n\n', 1)[1])
        self.assertEqual(FakeNative.writes[-1][0], 'manage_links.add')
        rows = [{'flag_id': f} for f in other['flag_ids']]
        bad = {k: other[k] for k in kit.ITEM_SCHEMA['required']}
        with self.assertRaisesRegex(kit.Error, 'existing friction record'):
            kit.validate_items([bad], rows, [{'id': created}, {'id': 'target'}], set(), friction=set())

    def test_briefing_covers_everything_unhandled_across_reviews(self):
        first = self.prepare()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE salient_flags SET text='Reworded' WHERE flag_id=10")
        with contextlib.redirect_stdout(io.StringIO()):
            kit.review(self.home, self.config, new_only=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kit.main(['--home', str(self.home), 'briefing'])
        text = out.getvalue()
        self.assertEqual(text.count('Review file:'), 2)  # newest review for flag 10, the older one for the rest
        self.assertNotIn('Family conversation', text)  # keep-out advice is a count in the briefing
        self.assertIn('kept out of Native', text)
        self.assertEqual(FakeNative.writes, [])

    def test_refusal_is_definite_not_uncertain(self):
        plan = self.prepare()
        item = plan['items'][0]
        def refuse(*a, **k):
            raise nc.NativeRefusal('kernel-v2 write: id is only supported for messaging sends')
        with patch.object(FakeNative, 'call', side_effect=lambda ex, op, args: refuse() if ex == 'records_write' else {'rows': []}):
            with self.assertRaisesRegex(kit.Error, 'nothing was written'):
                self.approve(plan, [item['number']])
        self.assertEqual(kit.state_load(self.home)['writes'], {})
        self.approve(plan, [item['number']])  # a later attempt is not blocked
        self.assertEqual(len(FakeNative.writes), 1)

    def test_uncertain_commit_blocks_replay_then_reconciles(self):
        plan = self.prepare()
        item = plan['items'][0]
        FakeNative.fail_after_commit = True
        with self.assertRaises(nc.SyncError):
            self.approve(plan, [item['number']])
        self.assertEqual(kit.state_load(self.home)['writes'][item['write_id']]['status'], 'uncertain')
        with self.assertRaisesRegex(kit.Error, 'Uncertain earlier write'):
            self.approve(plan, [item['number']])
        with contextlib.redirect_stdout(io.StringIO()):
            kit.reconcile(self.home, self.config, plan)
        self.assertEqual(kit.state_load(self.home)['writes'][item['write_id']]['status'], 'sent')
        self.approve(plan, [item['number']])
        self.assertEqual(len(FakeNative.writes), 1)

    def test_new_review_cannot_bypass_an_uncertain_write(self):
        first = self.prepare()
        item = first['items'][0]
        FakeNative.fail_after_commit = True
        with self.assertRaises(nc.SyncError):
            self.approve(first, [item['number']])
        second = self.prepare()
        FakeNative.fail_after_commit = False
        with self.assertRaisesRegex(kit.Error, 'earlier review has an uncertain'):
            self.approve(second, [second['items'][0]['number']])
        self.assertEqual(len(FakeNative.writes), 1)

    def test_schedule_is_opt_in_and_never_applies(self):
        self.assertFalse((self.root / 'Library/LaunchAgents').exists())
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(kit.sys, 'platform', 'darwin'), patch.object(kit.Path, 'home', return_value=self.root), \
             patch.object(kit.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            kit.schedule(self.home, self.config, True)
            plist = self.root / 'Library/LaunchAgents' / (kit.LABEL + '.plist')
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data['ProgramArguments'][-2:], ['review','--new-only'])
            self.assertNotIn('apply', data['ProgramArguments'])
            self.assertEqual(data['StartInterval'], 3600)
            self.assertEqual((self.home / 'schedule.log').stat().st_mode & 0o777, 0o600)
            kit.schedule(self.home, self.config, False)
            self.assertFalse(plist.exists())
        self.assertEqual(calls[0][1], 'bootstrap')
        self.assertEqual(calls[-1][1], 'bootout')

    def test_later_returns_after_expiry_and_dismissal_is_local(self):
        self.prepare()
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'decide', '1001', 'later', '--hours', '1'])
            kit.main(['--home', str(self.home), 'decide', '1002', 'dismissed'])
        state = kit.state_load(self.home)
        self.assertEqual(kit.flags(self.config, state, True)[0], [])
        with patch.object(kit.time, 'time', return_value=time.time() + 7200):
            rows, _ = kit.flags(self.config, state, True)
        self.assertEqual([r['flag_id'] for r in rows], [1001])
        self.assertEqual(FakeNative.writes, [])

    def test_digest_detects_edit_and_reinstall_preserves_customisations(self):
        plan = self.prepare()
        path = self.home / 'reviews' / (plan['id']+'.json')
        plan['items'][0]['text'] = 'Edited proposal'
        kit.write(path, plan)
        with self.assertRaisesRegex(kit.Error, 'Review changed'):
            kit.read_plan(path)
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'seal', str(path)])
        self.assertEqual(kit.read_plan(path)['items'][0]['text'], 'Edited proposal')
        skill = self.root / 'skills/goldfish-review/SKILL.md'
        skill.write_text('My own workflow')
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'install'])
        self.assertEqual(skill.read_text(), 'My own workflow')
        self.assertEqual(kit.configuration(self.home)['collection_id'], 'collection')
        self.assertTrue((self.home / 'kit/goldfish_review.py').exists())
        result = subprocess.run([str(self.home / 'workspace/goldfish'), 'pending'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([r for g in json.loads(result.stdout)['groups'] for r in g['flags']]), 34)

    def test_install_adds_recall_skill_without_replacing_one(self):
        recall = self.root / 'skills/goldfish/SKILL.md'
        self.assertIn('Goldfish and Native', recall.read_text())
        recall.write_text('Goldfish supplied this')
        with contextlib.redirect_stdout(io.StringIO()):
            kit.main(['--home', str(self.home), 'install'])
        self.assertEqual(recall.read_text(), 'Goldfish supplied this')

    def test_archive_contains_only_recipient_files_and_lock_prevents_overlap(self):
        archive = self.root / 'kit.tar.gz'
        package_kit.package(archive)
        with tarfile.open(archive) as f:
            names = f.getnames()
            self.assertEqual(len(names), 8)
            self.assertFalse(any('fixture' in n or 'auth' in n or 'state' in n for n in names))
            config = json.load(f.extractfile('goldfish-review-kit/config.default.json'))
            self.assertIsNone(config['collection_id'])
            self.assertIsNone(config['native_mcp_url'])
            self.assertTrue(config['review']['auto_write'])  # record by default
        with kit.locked(self.home):
            with self.assertRaises(kit.Error):
                with kit.locked(self.home):
                    pass

    def test_review_reports_progress_while_it_works(self):
        self.config.update(native_mcp_url='http://fake-native.invalid/mcp', collection_id='collection')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kit.review(self.home, self.config)
        self.assertRegex(out.getvalue(), r'\[\d\d:\d\d:\d\d\] 34 new flags to review')
        self.assertIn('Asking Claude to judge them', out.getvalue())

    def stopped_reviewer(self, reason, **patches):
        # A reviewer that would outlive us, with a grandchild like Claude's MCP server.
        marker = self.root / 'grandchild.pid'
        script = ('import subprocess,sys,time\n'
                  'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                  'open(%r, "w").write(str(p.pid)); time.sleep(60)\n' % str(marker))
        started = time.monotonic()
        with self.assertRaises(reason), contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(patch.object(kit.os, name, value))
                kit.run_claude([sys.executable, '-c', script], '{}', str(self.root), patches and 30 or 2)
        self.assertLess(time.monotonic() - started, 20)
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail('reviewer grandchild still running')

    def test_timeout_stops_the_whole_reviewer(self):
        self.stopped_reviewer(subprocess.TimeoutExpired)

    def test_losing_the_caller_stops_the_reviewer(self):
        parents = iter([111])
        self.stopped_reviewer(kit.Interrupted, getppid=lambda: next(parents, 1))


class ReaderTests(unittest.TestCase):
    def test_reader_exposes_only_reads_and_logs_what_was_seen(self):
        import native_reader
        calls = []
        class Fake:
            def tool(self, name, arguments):
                calls.append((name, arguments))
                if name == 'bootstrap':
                    return {'run': {'run_key': 'k'}}
                if arguments.get('operation') == 'query_sql':
                    return {'rows': [{'executor': 'sql_read', 'operation': 'query_sql'}]}
                if arguments['operation'] == 'search':
                    return {'hits': [{'id': 'r1', 'type': 'WorkItem', 'name': 'Docs blocker', 'snippet': 'x'}]}
                return {}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'reads.jsonl'
            r = native_reader.Reader.__new__(native_reader.Reader)
            r.native, r.log_path, r.run_key, r.readable = Fake(), log, None, None
            requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                         'params': {'name': 'search_native', 'arguments': {'query': 'Docs'}}},
                        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                         'params': {'name': 'create_record', 'arguments': {}}},
                        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'native_read',
                         'arguments': {'executor': 'records_write', 'operation': 'create_record'}}},
                        {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {'name': 'native_read',
                         'arguments': {'executor': 'sql_read', 'operation': 'query_sql',
                                       'arguments': {'sql': 'SELECT 1', 'parameters': []}}}}]
            out = io.StringIO()
            native_reader.serve(r, io.StringIO('\n'.join(json.dumps(m) for m in requests)), out)
            replies = [json.loads(l) for l in out.getvalue().splitlines()]
            self.assertEqual([m['id'] for m in replies], [1, 2, 3, 4, 5, 6])
            self.assertFalse(replies[5]['result'].get('isError'), replies[5]['result'])
            self.assertTrue(replies[4]['result']['isError'])
            self.assertIn('not a Native read operation', replies[4]['result']['content'][0]['text'])
            self.assertEqual({t['name'] for t in replies[1]['result']['tools']},
                             {'search_native', 'read_native_record', 'list_native_children', 'list_native_collections',
                              'native_read'})
            self.assertIn('Docs blocker', replies[2]['result']['content'][0]['text'])
            self.assertTrue(replies[3]['result']['isError'])
            self.assertEqual(json.loads(log.read_text().splitlines()[0])['seen'][0]['id'], 'r1')
        executors = {a.get('operation') for n, a in calls if n != 'bootstrap'}
        self.assertEqual(executors, {'set_intent', 'search', 'query_sql'})
        self.assertIn(('call_executor', {'executor': 'sql_read', 'operation': 'query_sql', 'format': 'json',
                       'arguments': {'sql': 'SELECT 1', 'parameters': []}, 'run_key': 'k'}), calls)


class NativeWireTests(unittest.TestCase):
    def test_reused_oauth_client_envelope_user_agent_and_run_continuity(self):
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((packet['params'], dict(self.headers)))
                name = packet['params']['name']
                result = {'run': {'run_key': 'test-run', 'durable': True, 'read_only': False}} if name == 'bootstrap' else {'rows': []}
                body = json.dumps({'jsonrpc':'2.0', 'id':packet['id'], 'result':{'structuredContent':result}}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(nc, 'AUTH', Path(tmp)/'auth.json'):
                    kit.write(nc.AUTH, {'access_token':'test', 'expires_at':time.time()+3600})
                    native = kit.Native({'native_mcp_url':'http://127.0.0.1:%d/mcp' % server.server_port})
                    native.start('Test fixed context read')
                    native.call('sql_read','query_sql',{'sql':'SELECT id FROM records ORDER BY id LIMIT 1'})
                    native.close()
            self.assertEqual(requests[0][0]['name'], 'bootstrap')
            self.assertEqual(requests[2][0]['arguments']['run_key'], 'test-run')
            self.assertEqual(requests[2][0]['arguments']['operation'], 'query_sql')
            self.assertTrue(requests[0][1]['User-Agent'].startswith('goldfish-review/'))
            self.assertEqual(requests[0][1]['Authorization'], 'Bearer test')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
