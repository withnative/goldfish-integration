#!/usr/bin/env python3
"""Local Goldfish review kit. Python 3.9+, standard library only."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import native_client as nc
import native_reader as reader

ROOT = Path(__file__).resolve().parent
LABEL = 'ai.withnative.goldfish-review'


class Error(Exception):
    pass


class Interrupted(Exception):
    pass


def progress(message):
    # Timestamped so a slow review is visibly alive, and so the scheduled log says when each run happened.
    print('[%s] %s' % (time.strftime('%H:%M:%S'), message), flush=True)


def run_claude(command, payload, directory, timeout):
    """Run the reviewer in its own process group so an interrupt, timeout or lost caller stops all of it."""
    parent = os.getppid()
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=directory, start_new_session=True)
    started = time.monotonic()
    feed, beat = payload, 60
    try:
        while True:
            try:
                out, err = child.communicate(feed, timeout=1)
                return subprocess.CompletedProcess(command, child.returncode, out, err)
            except subprocess.TimeoutExpired:
                feed = None
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            if os.getppid() != parent:
                # Whatever started us has gone (e.g. an agent's interrupted shell); don't carry on unseen.
                raise Interrupted('the program that started this review has stopped')
            if elapsed >= beat:
                progress('Claude is still reviewing (%d min so far)...' % (elapsed // 60))
                beat += 60
    finally:
        if child.poll() is None:
            for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(grace)
                    break


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def write(path, value):
    nc.private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(path, default=None):
    return nc.load_json(path, default)


@contextlib.contextmanager
def locked(home):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (home / 'lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('Another kit command is running; try again when it finishes.')
        yield


def configuration(home):
    config = load(home / 'config.json')
    if not config:
        raise Error('Run install first.')
    visible = config['lanes']['agent_visible']
    if not isinstance(visible, list) or set(visible) - {'work', 'careful'}:
        raise Error('agent_visible must contain only work and/or careful.')
    if config['review']['exclusions'] not in ('explicit', 'counts'):
        raise Error('review.exclusions must be explicit or counts.')
    if not 1 <= config['review']['max_flags'] <= 1000:
        raise Error('max_flags must be between 1 and 1000.')
    return config


def state_load(home):
    return load(home / 'state.json', {'decisions': {}, 'reviewed': {}, 'writes': {}})


def lane(row, config):
    rules = config['lanes']
    app, domain = (row.get(k) or '' for k in ('app', 'domain'))
    app, domain = app.casefold(), domain.casefold().rstrip('.')
    matches = lambda key, value: value in [s.casefold().rstrip('.') for s in rules[key]]
    if not row['snapshot_id'] or matches('careful_apps', app) or matches('careful_domains', domain):
        return 'careful'
    if matches('work_apps', app) or matches('work_domains', domain):
        return 'work'
    return 'careful'


def flags(config, state, new_only=False):
    path = Path(config['database']).expanduser().resolve()
    # mode=ro is load-bearing: missing files cannot be created, and Goldfish is never written.
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute('''SELECT f.flag_id, f.kind, f.text, f.snapshot_id,
            f.created_at, f.period_key, s.app, s.domain, s.window_title
            FROM salient_flags f LEFT JOIN snapshots s ON s.snapshot_id=f.snapshot_id
            WHERE f.status='open' ORDER BY f.flag_id''').fetchall()
    finally:
        db.close()
    result, hidden = [], 0
    for raw in rows:
        row = dict(raw)
        row['lane'] = lane(row, config)
        # Do not expose hidden-lane text, titles or metadata to the caller/model.
        if row['lane'] not in config['lanes']['agent_visible']:
            hidden += 1
            continue
        row['fingerprint'] = digest({k: v for k, v in row.items() if k != 'lane'})
        key = str(row['flag_id'])
        decision = state['decisions'].get(key, {})
        if decision.get('fingerprint') == row['fingerprint']:
            if decision['status'] in ('sent', 'dismissed'):
                continue
            if decision.get('until', 0) > time.time():
                continue
        if (new_only and state['reviewed'].get(key) == row['fingerprint']
                and not (decision.get('status') == 'later' and decision.get('until', 0) <= time.time())):
            continue
        result.append(row)
    limit = config['review']['max_flags']
    return result[:limit], {'hidden_lane_count': hidden, 'remaining': max(0, len(result)-limit)}


def extra_evidence(config):
    """Goldfish's open loops and calendar-like items, as read-only context (not reviewed one by one)."""
    path = Path(config['database']).expanduser().resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        def visible(snapshot_id):
            row = db.execute('SELECT app, domain FROM snapshots WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            meta = dict(row) if row else {}
            return lane(dict(meta, snapshot_id=snapshot_id if row else None), config) in config['lanes']['agent_visible']
        loops = [dict(r) for r in db.execute(
            "SELECT loop_id, kind, summary, project, person, due_at, confidence, first_seen_at, last_seen_at "
            "FROM open_loops WHERE status='open' ORDER BY last_seen_at DESC LIMIT 50")]
        attention = [dict(r) for r in db.execute(
            "SELECT item_id, kind, title, participants, summary, starts_at, ends_at, status, source_snapshot_id "
            "FROM attention_items WHERE status IN ('active','tentative') ORDER BY starts_at DESC LIMIT 50")]
        attention = [a for a in attention if visible(a.pop('source_snapshot_id'))]
        hidden_loops = 'careful' not in config['lanes']['agent_visible']
    finally:
        db.close()
    return {'goldfish_open_loops': [] if hidden_loops else loops, 'goldfish_attention': attention}


def groups(rows):
    # Exact normalized duplicates only; semantic grouping is the reviewing agent's job.
    result = {}
    for row in rows:
        key = (row['lane'], row['kind'], ' '.join(row['text'].casefold().split()))
        if key not in result:
            result[key] = {'lane': row['lane'], 'kind': row['kind'], 'text': row['text'], 'flags': []}
        result[key]['flags'].append(row)
    return list(result.values())


class Native(nc.Native):
    def __init__(self, config):
        if not config.get('native_mcp_url'):
            raise Error('Set native_mcp_url to your own Native MCP endpoint in config.json.')
        super().__init__(config)
        self.run_key = None

    def start(self, intent, writing=False):
        boot = self.tool('bootstrap', {'format': 'json'})
        run = boot.get('run', {})
        if writing and (not run.get('durable') or run.get('read_only') or not run.get('run_key')):
            raise Error('Native is not available for canonical work; no write attempted.')
        self.run_key = run.get('run_key')
        try:
            self.call('coordination_write', 'set_intent', {'intent': intent})
        except nc.SyncError:
            if writing:
                raise
            # Reads still work while Native is read-only (e.g. maintenance); the intent is a courtesy.

    def call(self, executor, operation, arguments):
        envelope = {'operation': operation, 'arguments': arguments, 'format': 'json'}
        if self.run_key:
            envelope['run_key'] = self.run_key
        return self.tool(executor, envelope)

    def record(self, record_id):
        data = self.call('records_read', 'get_record', {'ids': [record_id]})
        records = data.get('records', [])
        return records[0] if records and records[0].get('status') == 'found' else None

    def close(self):
        if self.run_key:
            try:
                self.call('coordination_write', 'close_run', {})
            except Exception:
                print('Native run could not be closed; recorded write receipts remain valid.', file=sys.stderr)


def native_context(config):
    if not config.get('native_mcp_url') or not config.get('collection_id'):
        return [], 'Native not configured: suggest creations with destination unset; do not guess records.'
    native = Native(config)
    try:
        native.start('Read existing work for a local Goldfish review; no Goldfish content is saved to Native.')
        home = native.record(config['collection_id'])
        if not home or home.get('type') != 'Collection' or home.get('archived'):
            raise Error('collection_id must resolve to a live Collection.')
        limit = config['review']['native_context_limit']
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise Error('native_context_limit must be between 1 and 200.')
        result = native.call('sql_read', 'query_sql', {
            'sql': 'SELECT id,type,name,summary FROM records WHERE home_id=?1 AND archived=0 '
                   'ORDER BY last_activity_at DESC,id LIMIT %d' % limit,
            'parameters': [home['id']]})
        return result.get('rows', []), 'Bounded direct children only; absence is not proof no matching record exists.'
    finally:
        native.close()


def friction_context(config):
    """The kit's own friction records, with their stable issue keys, so repeat sightings add evidence."""
    if not config.get('native_mcp_url') or not config.get('friction_collection_id'):
        return []
    native = Native(config)
    try:
        native.start('Read existing friction records for a local Goldfish review; no Goldfish content is saved.')
        rows = native.call('sql_read', 'query_sql', {
            'sql': "SELECT r.id, r.type, r.name, r.summary, r.lifecycle, f.value AS issue_key FROM records r "
                   "LEFT JOIN facet_values f ON f.record_id=r.id AND f.key='goldfish_issue' "
                   "WHERE r.home_id=?1 AND r.archived=0 ORDER BY r.last_activity_at DESC LIMIT 200",
            'parameters': [config['friction_collection_id']]}).get('rows', [])
    finally:
        native.close()
    return rows


ITEM_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'flag_ids': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1},
    'recommendation': {'enum': ['include', 'exclude', 'uncertain']},
    'reason': {'type': 'string'}, 'description': {'type': 'string'},
    'action': {'enum': ['create', 'comment', 'update', 'evidence', 'none']},
    'target_id': {'type': ['string', 'null']}, 'destination_id': {'type': ['string', 'null']},
    'name': {'type': 'string'}, 'text': {'type': 'string'},
    'basis': {'enum': ['captured', 'inferred']}, 'auto': {'type': 'boolean'},
    'issue_key': {'type': 'string'},
    'links': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
              'properties': {'target_id': {'type': 'string'}, 'note': {'type': 'string'}},
              'required': ['target_id', 'note']}}}, 'required': [
        'flag_ids', 'recommendation', 'reason', 'description', 'action', 'target_id', 'destination_id',
        'name', 'text', 'basis', 'auto', 'issue_key', 'links']}
# create/comment are undoable by archiving; evidence appends only to the kit's own friction records.
# update appends to someone else's body and always asks.
AUTO_ACTIONS = ('create', 'comment', 'evidence')
APPEND_ACTIONS = ('update', 'evidence')
REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'properties': {'items': {'type': 'array', 'items': ITEM_SCHEMA}}, 'required': ['items']}


def validate_items(items, rows, context, collections=(), friction=None):
    # context: records Claude was given or read; collections: allowed new-task destinations;
    # friction: the kit's own friction records (None skips the check, e.g. for saved plans).
    expected = {r['flag_id'] for r in rows}
    targets = {r['id'] for r in context}
    collections = set(collections)
    seen = set()
    for item in items:
        if set(item) != set(ITEM_SCHEMA['required']):
            raise Error('Review item has unexpected or missing fields.')
        ids = item['flag_ids']
        if not ids or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
            raise Error('Review flag IDs must be unique integers.')
        if set(ids) - expected or seen.intersection(ids):
            raise Error('Review references unknown or repeated flags.')
        seen.update(ids)
        if item['recommendation'] not in ('include', 'exclude', 'uncertain'):
            raise Error('Unknown recommendation.')
        if any(not isinstance(item[k], str) for k in ('reason', 'description', 'name', 'text')):
            raise Error('Review text fields must be strings.')
        if not item['description'].strip() or not item['reason'].strip():
            raise Error('Each recommendation needs a description and reason.')
        if item['basis'] not in ('captured', 'inferred') or type(item['auto']) is not bool:
            raise Error('Each recommendation needs a basis and an auto flag.')
        if item['auto'] and (item['recommendation'] != 'include' or item['action'] not in AUTO_ACTIONS):
            raise Error('Only a new task or comment can be written automatically.')
        if not isinstance(item['issue_key'], str) or not isinstance(item['links'], list):
            raise Error('issue_key must be a string and links a list.')
        if any(set(l) != {'target_id', 'note'} or l['target_id'] not in targets for l in item['links']):
            raise Error('Links must point at records Claude was given or read.')
        if item['action'] == 'evidence' and friction is not None and item['target_id'] not in friction:
            raise Error('Evidence can only be added to an existing friction record.')
        if item['recommendation'] == 'include':
            if item['action'] not in ('create', 'comment', 'update', 'evidence') or not item['text'].strip():
                raise Error('An inclusion needs a concrete action and exact text.')
            if item['action'] == 'create':
                if item['target_id'] is not None or not item['name'].strip():
                    raise Error('A new task needs a name and no existing target.')
                if item['destination_id'] is not None and item['destination_id'] not in collections:
                    raise Error('A new task destination must be a collection Claude actually saw.')
            elif item['target_id'] not in targets:
                raise Error('Existing target must be a record Claude was given or read.')
            elif item['destination_id'] is not None:
                raise Error('Only a new task carries a destination collection.')
        elif (item['action'] != 'none' or item['target_id'] is not None or item['destination_id'] is not None
              or item['links']):
            raise Error('Exclusions and uncertainty cannot carry a write action.')
    if seen != expected:
        raise Error('Review omitted flags; refusing to advance the review checkpoint.')


def skill_file(home, config):
    return Path(config.get('skill_path') or home / 'workspace/.claude/skills/goldfish-review/SKILL.md')


def review(home, config, new_only=False):
    state = state_load(home)
    rows, counts = flags(config, state, new_only)
    if not rows:
        print('No new eligible flags.' if new_only else 'No pending eligible flags.')
        return
    progress('%d new flag%s to review. Reading Native context...' % (len(rows), '' if len(rows) == 1 else 's'))
    context, context_note = native_context(config)
    friction_records = friction_context(config)
    context = context + friction_records
    skill = skill_file(home, config).read_text()
    brief = config['review'].get('brief', 'record')
    if brief != 'record':
        # Optional extra brief shipped with the kit, e.g. 'drift' for hygiene checks.
        extra = ROOT / 'skills/goldfish-review' / (brief + '.md')
        if not extra.exists():
            raise Error('Unknown review.brief %r.' % brief)
        skill += '\n\n' + extra.read_text()
    searching = bool(config['review'].get('native_search', True) and config.get('native_mcp_url'))
    payload = {'flags': groups(rows), 'native_context': context, 'context_note': context_note,
               'landing_collection': config.get('collection_id'), 'native_search': searching,
               'auto_write': bool(config['review'].get('auto_write')),
               'friction_collection': config.get('friction_collection_id'), 'friction_records': friction_records,
               'presentation': config['review']['exclusions']}
    if config['review'].get('include_loops'):
        payload.update(extra_evidence(config))
    command = config['review']['claude_command']
    if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
        raise Error('claude_command must be a JSON array of command arguments.')
    with tempfile.TemporaryDirectory(prefix='goldfish-review-') as directory:
        log = Path(directory) / 'native-reads.jsonl'
        log.touch()
        servers = {}
        if searching:
            # The only Native access Claude gets: the kit's own read-only tools, no write path.
            servers['native'] = {'command': sys.executable, 'args': [
                str(ROOT / 'native_reader.py'), '--home', str(home), '--log', str(log)]}
        # No built-in tools, shell, skill hooks or ambient MCP servers in the scheduled inference.
        command = command + ['-p', '--max-turns', str(config['review'].get('max_turns', 30)),
                             '--output-format', 'json', '--json-schema', json.dumps(REVIEW_SCHEMA),
                             '--tools', '', '--strict-mcp-config', '--mcp-config', json.dumps({'mcpServers': servers}),
                             '--setting-sources', '', '--disable-slash-commands', '--system-prompt', skill]
        if servers:
            command += ['--allowedTools', ','.join('mcp__native__' + t['name'] for t in reader.TOOLS)]
        if config['review']['model']:
            command += ['--model', config['review']['model']]
        progress('Asking Claude to judge them; this usually takes a few minutes...')
        run = run_claude(command, json.dumps(payload), directory, config['review']['timeout_seconds'])
        reads = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if run.returncode:
        raise Error('Claude review failed (exit %d); checkpoint unchanged. Check Claude authentication/version.' % run.returncode)
    nc.private_write(home / 'last-model-output.json', run.stdout)  # local, for debugging a rejected review
    answer = json.loads(run.stdout)
    if answer.get('is_error') or not isinstance(answer.get('structured_output'), dict):
        raise Error('Claude returned no structured review; checkpoint unchanged.')
    items = answer['structured_output'].get('items')
    if not isinstance(items, list):
        raise Error('Claude returned no item list.')
    for item in items:
        # Tidy harmless model slips instead of discarding a whole review: hints on items that
        # are not being written are dropped, and anything not automatically writable asks first.
        if isinstance(item, dict) and item.get('recommendation') in ('exclude', 'uncertain'):
            item.update(action='none', target_id=None, destination_id=None, text='', auto=False, links=[])
        elif isinstance(item, dict) and item.get('action') not in AUTO_ACTIONS:
            item['auto'] = False
        if isinstance(item, dict) and item.get('action') != 'create':
            item['destination_id'] = None
    seen = context + [r for entry in reads for r in entry['seen']]
    collections = {r['id'] for r in seen if r.get('type') == 'Collection'}
    collections.update({config.get('collection_id'), config.get('friction_collection_id')} - {None})
    targets = {r['id'] for r in seen}
    friction = {r['id'] for r in friction_records}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get('links'), list):
            kept = [l for l in item['links'] if isinstance(l, dict) and l.get('target_id') in targets]
            if len(kept) < len(item['links']):
                item['reason'] = str(item.get('reason', '')) + ' (Dropped links to records Claude did not read.)'
            item['links'] = kept
        if isinstance(item, dict) and item.get('action') == 'evidence' and item.get('target_id') not in friction:
            item['action'] = 'comment'  # not one of the kit's friction records: say it, don't append
        # An unverifiable destination falls back to the landing collection; an unverifiable
        # target becomes a question. One slip should not cost the whole review.
        if not isinstance(item, dict) or item.get('recommendation') != 'include':
            continue
        if item.get('action') == 'create' and item.get('destination_id') not in collections | {None}:
            item['reason'] = str(item.get('reason', '')) + ' (Suggested collection %s was not one Claude read; using the fallback.)' % item['destination_id']
            item['destination_id'] = None
        elif item.get('action') in ('comment', 'update') and item.get('target_id') not in targets:
            item['reason'] = str(item.get('reason', '')) + ' (Proposed %s on %s, a record Claude did not read; check before writing.)' % (item['action'], item.get('target_id'))
            item.update(recommendation='uncertain', action='none', target_id=None, destination_id=None, text='', auto=False)
    validate_items(items, rows, seen, collections, friction)
    source_by_id = {r['flag_id']: r for r in rows}
    native = Native(config) if any(i['recommendation'] == 'include' for i in items) else None
    if native:
        progress('Checking proposed destinations in Native...')
    unresolved = False
    try:
        if native:
            try:
                native.start('Read proposed destinations and revision guards for a local Goldfish review; no Goldfish content is saved.')
            except nc.SyncError:
                # Keep the finished review; apply re-checks every destination before writing.
                native, unresolved = None, True
        for number, item in enumerate(items, 1):
            item['number'] = number
            item['write_id'] = str(uuid.uuid4())
            item['sources'] = [{k: source_by_id[i][k] for k in (
                'flag_id', 'fingerprint', 'kind', 'lane', 'app', 'domain', 'created_at')} for i in item['flag_ids']]
            item['if_body_digest'] = None
            item['destination_name'] = None
            if item['recommendation'] == 'include' and native:
                try:
                    resolve(native, config, item)
                except nc.SyncError:
                    unresolved = True
    finally:
        if native:
            native.close()
    if unresolved:
        print('Native was unavailable after the review; destinations will be checked when applied.')
    plan = {'version': 2, 'id': str(uuid.uuid4()), 'created_at': int(time.time()),
            'native_mcp_url': config.get('native_mcp_url'), 'collection_id': config.get('collection_id'),
            'native_reads': [{'tool': e['tool'], 'arguments': e['arguments'], 'results': len(e['seen'])} for e in reads],
            'items': items, 'counts': counts}
    plan['digest'] = digest(plan)
    path = home / 'reviews' / (plan['id'] + '.json')
    write(path, plan)
    # Persist result first: an interruption may repeat a review, never silently lose a batch.
    for row in rows:
        state['reviewed'][str(row['flag_id'])] = row['fingerprint']
    write(home / 'state.json', state)
    nc.private_write(home / 'latest.txt', str(path) + '\n')
    automatic = [i['number'] for i in items if i['auto']] if config['review'].get('auto_write') else []
    automatic = automatic[:config['review'].get('auto_max_per_run', 10)]
    if automatic:
        try:
            apply(home, config, plan, automatic, None, automatic=True)
        except (Error, nc.SyncError) as error:
            # The review is saved either way; unsent items stay pending for the person.
            print('Automatic writes stopped: %s' % error)
    show(plan, config, state_load(home))
    print('Review file: %s\nApproval digest: %s' % (path, plan['digest']))
    if config['review']['notify'] and sys.platform == 'darwin':
        # Generic notification: counts only. Sensitive text stays in the local review, never on the lock screen.
        added = len(automatic)
        tally = {k: sum(i['recommendation'] == k for i in items) for k in ('include', 'uncertain')}
        message = '%sadded, %d to approve, %d need your judgement. Ask Claude: what\'s new from Goldfish?' % (
            '%d ' % added, tally['include'] - added, tally['uncertain'])
        subprocess.run(['osascript', '-e', 'display notification %s with title "Goldfish review"' % json.dumps(message)],
                       capture_output=True, timeout=15, check=False)


def read_plan(path):
    plan = load(path)
    if not isinstance(plan, dict) or plan.get('version') != 2:
        raise Error('Not a version 2 review file; prepare a new review.')
    without = {k: v for k, v in plan.items() if k != 'digest'}
    if plan.get('digest') != digest(without):
        raise Error('Review changed: regenerate its digest with seal, then review the new exact content.')
    return plan


def status(item, state):
    journal = (state or {}).get('writes', {}).get(item['write_id'], {})
    return journal.get('status'), journal.get('record_id')


def show(plan, config, state=None, items=None):
    state = state_load_default() if state is None else state
    items = plan['items'] if items is None else items
    sent = [i for i in items if status(i, state)[0] in ('sent', 'undone')]
    unsent = [i for i in items if i not in sent]
    print('Goldfish review — only items listed under "Added to Native" have been written.')
    if sent:
        print('\nAdded to Native (%d)' % len(sent))
        for item in sent:
            how, record_id = status(item, state)
            print('[%s] %s — %s%s\n  record %s%s' % (
                item['number'], item['description'], 'automatically, ' if item['auto'] else '', item['basis'],
                record_id, ' (undone)' if how == 'undone' else '  — undo: ./goldfish undo %s' % record_id))
    for category, heading in [('include', 'Suggest adding to Native'), ('exclude', 'Suggest keeping out of Native'),
                              ('uncertain', 'Need your judgement')]:
        chosen = [i for i in unsent if i['recommendation'] == category]
        print('\n%s (%d)' % (heading, len(chosen)))
        if category == 'exclude' and config['review']['exclusions'] == 'counts':
            continue
        for item in chosen:
            print('[%s] %s — %s' % (item['number'], item['description'], item['reason']))
            if category == 'include':
                where = item['target_id'] or item['destination_id'] or plan['collection_id']
                label = '%s (%s)' % (item['destination_name'], where) if item.get('destination_name') else where
                print('  %s -> %s\n%s' % (item['action'], label or 'DESTINATION UNSET', item['text'] + provenance(item, False)))
                if item['action'] == 'create':
                    print('  Task title: %s' % item['name'])
                print('  Goldfish flags: %s' % ', '.join(map(str, item['flag_ids'])))
    if plan.get('native_reads'):
        print('\nClaude read Native %d times: %s' % (len(plan['native_reads']), '; '.join(
            e['arguments'].get('query') or e['tool'] for e in plan['native_reads'])))
    if plan['counts']['hidden_lane_count']:
        print('\n%d flags hidden by your lane visibility configuration.' % plan['counts']['hidden_lane_count'])
    if plan['counts']['remaining']:
        print('%d more flags remain for the next batch.' % plan['counts']['remaining'])


def state_load_default():
    return {'writes': {}}


def resolve(native, config, item):
    """Confirm a proposal's real destination and record its name for display."""
    if item['action'] == 'create':
        record = native.record(item['destination_id'] or config.get('collection_id')) if (
            item['destination_id'] or config.get('collection_id')) else None
        if item['destination_id'] and (not record or record.get('type') != 'Collection' or record.get('archived')):
            raise Error('Proposed destination is not a live Collection; review again.')
    else:
        record = native.record(item['target_id'])
        if not record or record.get('archived'):
            raise Error('Proposed target is unavailable; review again.')
        if item['action'] in APPEND_ACTIONS:
            item['if_body_digest'] = record['body_digest']
    item['destination_name'] = record.get('name') if record else None


BASIS = {'captured': 'captured: Goldfish recorded this on screen; not independently checked.',
         'inferred': "inferred: Claude's reading of Goldfish captures and Native context; may be wrong."}


def provenance(item, automatic=None):
    automatic = item.get('auto') if automatic is None else automatic
    lines = ['\n\nSource: Goldfish screen memory, %s.' % (
                 'written automatically by Goldfish review, not checked by a person' if automatic
                 else 'approved in Goldfish review'),
             'Basis: ' + BASIS[item.get('basis', 'inferred')]]
    for s in item['sources']:
        stamp = datetime.datetime.fromtimestamp(s['created_at'], datetime.timezone.utc).isoformat()
        lines.append('- Flag %s | %s | %s | %s | %s' % (
            s['flag_id'], s['kind'], s['app'] or 'Goldfish chat/unknown', stamp, s['lane']))
    return '\n'.join(lines)


def validate_plan(plan):
    items = plan['items']
    rows, context, collections = [], [], set()
    for item in items:
        if set(item['flag_ids']) != {s['flag_id'] for s in item['sources']}:
            raise Error('Source metadata does not match proposal flag IDs.')
        rows.extend({'flag_id': s['flag_id']} for s in item['sources'])
        if item['target_id']:
            context.append({'id': item['target_id']})
        context.extend({'id': l['target_id']} for l in item['links'])
        collections.add(item['destination_id'])
    base = [{k: item[k] for k in ITEM_SCHEMA['required']} for item in items]
    validate_items(base, rows, context, collections)
    if len({i['number'] for i in items}) != len(items):
        raise Error('Duplicate review item numbers.')
    if len({i['write_id'] for i in items}) != len(items):
        raise Error('Duplicate write IDs.')
    for item in items:
        if str(uuid.UUID(item['write_id'], version=4)) != item['write_id']:
            raise Error('write_id must be a UUIDv4.')


def apply(home, config, plan, numbers, approval, automatic=False):
    validate_plan(plan)
    if automatic:
        # Record-by-default: only items Claude marked auto, only when the person turned it on.
        if not config['review'].get('auto_write'):
            raise Error('Automatic writes are off (review.auto_write).')
        if any(not i['auto'] for i in plan['items'] if i['number'] in numbers):
            raise Error('Only items marked for automatic writing can be sent without approval.')
    elif approval != plan['digest']:
        raise Error('Explicit approval must name the digest of the exact reviewed plan.')
    if (config.get('native_mcp_url'), config.get('collection_id')) != (plan['native_mcp_url'], plan['collection_id']):
        raise Error('Native endpoint/destination changed; prepare a new review.')
    if not numbers or len(set(numbers)) != len(numbers):
        raise Error('Select unique review item numbers.')
    selected = [i for i in plan['items'] if i['number'] in numbers]
    if len(selected) != len(numbers) or any(i['recommendation'] != 'include' for i in selected):
        raise Error('Only concrete include recommendations can be applied.')
    state = state_load(home)
    selected_flags = {s['flag_id'] for item in selected for s in item['sources']}
    selected_writes = {i['write_id'] for i in selected}
    for write_id, journal in state['writes'].items():
        if (write_id not in selected_writes and journal['status'] == 'uncertain'
                and selected_flags.intersection(journal.get('flag_ids', []))):
            raise Error('An earlier review has an uncertain write for these flags; reconcile that review first.')
    current, _ = flags(dict(config, review=dict(config['review'], max_flags=sys.maxsize)), state)
    current = {r['flag_id']: r['fingerprint'] for r in current}
    for item in selected:
        known = state['writes'].get(item['write_id'])
        if known and known.get('plan_digest') != plan['digest']:
            raise Error('This plan changed after a write attempt; prepare a new review.')
        if known and known['status'] == 'sent':
            continue
        if known:
            raise Error('Uncertain earlier write. Run reconcile on this review before retrying.')
        if any(current.get(s['flag_id']) != s['fingerprint'] for s in item['sources']):
            raise Error('Source flag changed, was handled, or is deferred/hidden. Review again.')
    native = Native(config)
    try:
        native.start('Apply explicitly approved Goldfish review actions; save only the approved content and provenance.', writing=True)
        for item in selected:
            if state['writes'].get(item['write_id'], {}).get('status') == 'sent':
                print('Already sent: item %s' % item['number'])
                continue
            text = item['text'] + provenance(item, automatic)
            facets = {'source': 'goldfish', 'goldfish_basis': item['basis'],
                      'goldfish_written': 'automatic' if automatic else 'approved'}
            if item['issue_key'].strip():
                facets['goldfish_issue'] = item['issue_key'].strip()
            related = [{'target_id': l['target_id'], 'relationship': 'relates_to', 'note': l['note'] or 'Related.'}
                       for l in item['links']]
            args = {'reason': ('Record-by-default: Goldfish review judged this worth recording; basis %s.' % item['basis']
                               if automatic else 'Apply the user-approved Goldfish review action.'), 'sources': [],
                    'idempotency_key': 'goldfish-review:' + item['write_id']}
            target = item['target_id']
            record_id = None  # creates: Native mints the id (KernelV2 refuses supplied ids); the key makes retries safe
            if item['action'] == 'create':
                destination = item['destination_id'] or config.get('collection_id')
                if not destination:
                    raise Error('Choose a landing collection before applying new tasks.')
                home_record = native.record(destination)
                if not home_record or home_record.get('type') != 'Collection' or home_record.get('archived'):
                    raise Error('Landing collection is not a live Collection.')
                args.update(type='WorkItem', kind='task', name=item['name'], body=text,
                            home_id=home_record['id'], facets=facets, links=related, lifecycle='open')
                operation = 'create_record'
                expected = nc.sha256(text)
            else:
                record = native.record(target)
                if not record or record.get('archived'):
                    raise Error('Target no longer available; review again.')
                args['sources'] = [{'record_id': record['id'], 'reason': 'Approved target for Goldfish-derived contribution.'}]
                if item['action'] == 'comment':
                    args.update(type='Annotation', kind='comment', body=text, facets=facets,
                                links=[{'target_id': record['id'], 'relationship': 'part_of'}])
                    operation, expected = 'create_record', nc.sha256(text)
                elif item['action'] in APPEND_ACTIONS:
                    if record['body_digest'] != item['if_body_digest']:
                        raise Error('Target edited since review; prepare a fresh proposal.')
                    record_id = record['id']
                    args.update(id=record_id, body_append='\n\n' + text, if_body_digest=item['if_body_digest'])
                    operation, expected = 'update_record', nc.sha256((record.get('body') or '') + '\n\n' + text)
                else:
                    raise Error('Unknown write action.')
            journal = {'status': 'uncertain', 'record_id': record_id, 'expected_body_digest': expected,
                       'operation': operation, 'plan_digest': plan['digest'], 'flag_ids': item['flag_ids'],
                       'automatic': automatic, 'at': int(time.time())}
            if operation == 'create_record':
                journal['replay'] = args  # local only: reconcile repeats it with the same idempotency key
            # Write-ahead journal: never blindly replay a timed-out or uncertain write.
            state['writes'][item['write_id']] = journal
            write(home / 'state.json', state)
            try:
                receipt = native.call('records_write', operation, args)
            except nc.NativeRefusal as refusal:
                # A refusal is a definite answer: nothing was written, so this is not an uncertain write.
                del state['writes'][item['write_id']]
                write(home / 'state.json', state)
                raise Error('Native refused item %s, nothing was written: %s' % (item['number'], refusal))
            record_id = record_id or receipt.get('id')
            if not record_id or receipt.get('id') != record_id or not receipt.get('write_receipt', {}).get('event'):
                raise Error('No canonical write receipt; run reconcile before retrying.')
            journal['record_id'] = record_id
            if item['action'] in APPEND_ACTIONS or item['action'] == 'comment':
                # Appends and comments carry their links separately (Native refuses a comment created with
                # links beyond its part_of bearer); keyed, so safe to repeat.
                for link in related:
                    native.call('records_write', 'manage_links.add', dict(
                        source_id=record_id, target_id=link['target_id'], relationship='relates_to',
                        note=link['note'], idempotency_key='goldfish-link:%s:%s' % (item['write_id'], link['target_id'])))
            finish(state, item, record_id)
            write(home / 'state.json', state)
            print('Sent item %s -> %s' % (item['number'], record_id))
    finally:
        native.close()


def finish(state, item, record_id):
    state['writes'][item['write_id']]['status'] = 'sent'
    for s in item['sources']:
        state['decisions'][str(s['flag_id'])] = {'status': 'sent', 'fingerprint': s['fingerprint'], 'record_id': record_id}


def reconcile(home, config, plan):
    if (config.get('native_mcp_url'), config.get('collection_id')) != (plan['native_mcp_url'], plan['collection_id']):
        raise Error('Reconcile requires the original Native endpoint and collection.')
    state = state_load(home)
    native = Native(config)
    try:
        native.start('Reconcile uncertain Goldfish writes by reading canonical records; do not replay writes.')
        for item in plan['items']:
            journal = state['writes'].get(item['write_id'])
            if not journal or journal['status'] != 'uncertain':
                continue
            if journal['plan_digest'] != plan['digest']:
                raise Error('Reconcile requires the unchanged original review.')
            if journal['operation'] == 'create_record' and journal.get('replay'):
                # Native returns the record already created for a repeated idempotency key, and creates
                # it only if the first attempt never landed, so this cannot duplicate.
                receipt = native.call('records_write', 'create_record', journal['replay'])
                if receipt.get('id'):
                    journal['record_id'] = receipt['id']
                    finish(state, item, receipt['id'])
                    write(home / 'state.json', state)
                    print('Confirmed sent: item %s -> %s' % (item['number'], receipt['id']))
                    continue
            found = native.record(journal['record_id']) if journal.get('record_id') else None
            if found and found['body_digest'] == journal['expected_body_digest']:
                finish(state, item, found['id'])
                write(home / 'state.json', state)
                print('Confirmed sent: item %s' % item['number'])
            else:
                print('Unresolved item %s: inspect Native history and the local write journal; no retry performed.' % item['number'])
    finally:
        native.close()


def undo(home, config, record_id):
    """Archive a record or comment this kit wrote, and keep its flags out of future reviews."""
    state = state_load(home)
    journal = next((j for j in state['writes'].values() if j.get('record_id') == record_id), None)
    if not journal or journal['status'] != 'sent':
        raise Error('Not a record this kit sent (or already undone).')
    if journal['operation'] != 'create_record':
        raise Error('Appended text cannot be archived; edit the record in Native instead.')
    native = Native(config)
    try:
        native.start('Undo a Goldfish review write at the person\'s request.', writing=True)
        native.call('records_lifecycle', 'archive_record', {
            'id': record_id, 'reason': 'The person undid this Goldfish-derived record.',
            'idempotency_key': 'goldfish-undo:' + record_id})
    finally:
        native.close()
    journal['status'] = 'undone'
    for flag_id in journal['flag_ids']:
        state['decisions'][str(flag_id)]['status'] = 'dismissed'
    write(home / 'state.json', state)
    print('Archived %s in Native (restorable there); its Goldfish flags are dismissed locally.' % record_id)


def briefing(home, config):
    """Everything still unhandled across saved reviews, newest judgement first, plus recent automatic writes."""
    state = state_load(home)
    pending, _ = flags(dict(config, review=dict(config['review'], max_flags=sys.maxsize)), state)
    pending = {r['flag_id']: r['fingerprint'] for r in pending}
    claimed, shown = set(), 0
    since = state.get('briefed_at', 0)
    paths = sorted((home / 'reviews').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            plan = read_plan(path)
        except Error:
            continue
        recent = [i for i in plan['items'] if status(i, state)[0] == 'sent' and i['auto']
                  and state['writes'][i['write_id']].get('at', 0) > since]
        live = [i for i in plan['items'] if status(i, state)[0] is None
                and all(pending.get(s['flag_id']) == s['fingerprint'] for s in i['sources'])
                and not claimed.intersection(i['flag_ids'])]
        for item in live:
            claimed.update(item['flag_ids'])
        # Keep-out advice was shown when the review was prepared; repeat it only as a count.
        kept_out = [i for i in live if i['recommendation'] == 'exclude']
        live = [i for i in live if i['recommendation'] != 'exclude']
        if kept_out:
            print('%d items kept out of Native in %s (./goldfish show %s to see them).' % (len(kept_out), path.name, path))
        if recent or live:
            shown += 1
            print('=' * 70)
            show(plan, config, state, recent + live)
            print('Review file: %s\nApproval digest: %s\n' % (path, plan['digest']))
    fresh = [i for i, f in pending.items() if i not in claimed and state['reviewed'].get(str(i)) != f]
    if not shown:
        print('Nothing unhandled in saved reviews.')
    if fresh:
        print('%d flags have not been reviewed yet: run ./goldfish review --new-only.' % len(fresh))
    state['briefed_at'] = int(time.time())
    write(home / 'state.json', state)


def install(home, args):
    config_path = home / 'config.json'
    if not config_path.exists():
        config = load(ROOT / 'config.default.json')
        if args.database:
            config['database'] = str(Path(args.database).expanduser().resolve())
        if args.collection:
            config['collection_id'] = args.collection
        if args.native_url:
            config['native_mcp_url'] = args.native_url
        write(config_path, config)
    elif args.database or args.collection or args.native_url:
        raise Error('Existing config preserved: edit config.json to change paths or Native destination.')
    # Copy the small runtime so the downloaded/extracted folder can be removed.
    runtime = home / 'kit'
    for relative in ('goldfish_review.py', 'native_client.py', 'native_reader.py', 'config.default.json',
                     'skills/goldfish-review/SKILL.md', 'skills/goldfish-review/drift.md', 'skills/goldfish/SKILL.md',
                     'README.md'):
        source, target = ROOT / relative, runtime / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if source.exists() and source.resolve() != target.resolve():
            nc.private_write(target, source.read_text())
    # A dedicated workspace; no global Claude settings or permissions are changed.
    work = home / 'workspace'
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    launcher = work / 'goldfish'
    # User-level skill, so any Claude session can answer "what's new from Goldfish?".
    config = load(config_path)
    skill = Path(args.skills_dir).expanduser() / 'goldfish-review/SKILL.md' if getattr(args, 'skills_dir', None) \
        else Path(config.get('skill_path') or Path.home() / '.claude/skills/goldfish-review/SKILL.md')
    if config.get('skill_path') != str(skill):
        config['skill_path'] = str(skill)
        write(config_path, config)
    skill.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not skill.exists():
        skill.write_text((ROOT / 'skills/goldfish-review/SKILL.md').read_text().replace('{{GOLDFISH}}', str(launcher)))
    # The recall skill too, unless one is already installed (Goldfish may supply its own).
    recall = skill.parent.parent / 'goldfish/SKILL.md'
    if not recall.exists():
        recall.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        recall.write_text((ROOT / 'skills/goldfish/SKILL.md').read_text())
    else:
        print('Kept existing %s; compare skills/goldfish/SKILL.md for its Native section.' % recall)
    import shlex
    launcher.write_text('#!/bin/sh\nexec %s %s --home %s "$@"\n' % (
            shlex.quote(sys.executable), shlex.quote(str(runtime / 'goldfish_review.py')), shlex.quote(str(home))))
    launcher.chmod(0o700)
    print('Installed config: %s\nClaude skill: %s\nNo schedule enabled.' % (config_path, skill))


def schedule(home, config, enable):
    if sys.platform != 'darwin':
        raise Error('launchd scheduling is available on macOS only.')
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    service = 'gui/%d/%s' % (os.getuid(), LABEL)
    if not enable:
        stopped = subprocess.run(['launchctl', 'bootout', service], capture_output=True, check=False)
        if stopped.returncode:
            status = subprocess.run(['launchctl', 'print', service], capture_output=True, check=False)
            if status.returncode == 0:
                raise Error('launchd job is still loaded; schedule file kept. Inspect launchctl before retrying.')
        plist.unlink(missing_ok=True)
        print('Hourly review disabled. Local reviews and config kept.')
        return
    command = config['review']['claude_command']
    executable = shutil.which(command[0])
    if not executable:
        raise Error('Claude executable not found; set review.claude_command to its absolute path.')
    config['review']['claude_command'][0] = executable
    interval = config['review']['interval_seconds']
    if type(interval) is not int or interval < 60:
        raise Error('interval_seconds must be an integer of at least 60.')
    if plist.exists():
        raise Error('Schedule already exists; disable it before changing the schedule.')
    write(home / 'config.json', config)
    for name in ('schedule.log', 'schedule-error.log'):
        if not (home / name).exists():
            nc.private_write(home / name, '')
        (home / name).chmod(0o600)
    data = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(ROOT / 'goldfish_review.py'),
        '--home', str(home), 'review', '--new-only'],
        'RunAtLoad': True, 'StandardOutPath': str(home / 'schedule.log'),
        'StandardErrorPath': str(home / 'schedule-error.log'),
        'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}}
    minute = config['review'].get('schedule_minute')
    if minute is None:
        data['StartInterval'] = interval
    elif type(minute) is int and 0 <= minute <= 59:
        # Hourly at a fixed minute, a little after Goldfish's hourly batch lands. launchd runs a
        # missed slot once on wake, so sleep is caught up rather than skipped.
        data['StartCalendarInterval'] = {'Minute': minute}
    else:
        raise Error('schedule_minute must be null or 0-59.')
    plist.parent.mkdir(parents=True, exist_ok=True)
    nc.private_write(plist, plistlib.dumps(data).decode())
    result = subprocess.run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(plist)], capture_output=True)
    if result.returncode:
        plist.unlink()
        raise Error('launchd refused the schedule; no schedule file retained.')
    print(('Scheduled review hourly at :%02d' % minute if minute is not None else
           'Scheduled review every %d seconds' % interval) + ' while the Mac is awake; apply is never scheduled.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.local/share/goldfish-review')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('install')
    p.add_argument('--database'); p.add_argument('--collection'); p.add_argument('--native-url')
    p.add_argument('--skills-dir', help='Claude skills directory (default ~/.claude/skills)')
    commands.add_parser('briefing')
    p = commands.add_parser('undo'); p.add_argument('record_id')
    p = commands.add_parser('pending'); p.add_argument('--new-only', action='store_true')
    p = commands.add_parser('review'); p.add_argument('--new-only', action='store_true')
    commands.add_parser('latest'); commands.add_parser('login'); commands.add_parser('doctor')
    p = commands.add_parser('show'); p.add_argument('plan', type=Path)
    p = commands.add_parser('seal'); p.add_argument('plan', type=Path)
    p = commands.add_parser('apply'); p.add_argument('plan', type=Path); p.add_argument('--items', required=True)
    p.add_argument('--approve', required=True, help='Digest of the exact plan explicitly approved by the user')
    p = commands.add_parser('reconcile'); p.add_argument('plan', type=Path)
    p = commands.add_parser('decide'); p.add_argument('flag_ids', help='Comma-separated Goldfish IDs')
    p.add_argument('decision', choices=['dismissed', 'later']); p.add_argument('--hours', type=float, default=24)
    p = commands.add_parser('schedule'); p.add_argument('action', choices=['enable', 'disable'])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    nc.AUTH = home / 'native-auth.json'
    with locked(home):
        if args.command == 'install':
            install(home, args); return
        config = configuration(home)
        if args.command == 'login':
            Native(config).login()
        elif args.command == 'pending':
            rows, counts = flags(config, state_load(home), args.new_only)
            print(json.dumps({'groups': groups(rows), **counts}, ensure_ascii=False, indent=2))
        elif args.command == 'review':
            review(home, config, args.new_only)
        elif args.command in ('show', 'latest'):
            path = args.plan if args.command == 'show' else Path((home / 'latest.txt').read_text().strip())
            plan = read_plan(path); show(plan, config, state_load(home))
            print('Review file: %s\nApproval digest: %s' % (path, plan['digest']))
        elif args.command == 'apply':
            apply(home, config, read_plan(args.plan), [int(i) for i in args.items.split(',')], args.approve)
        elif args.command == 'reconcile':
            reconcile(home, config, read_plan(args.plan))
        elif args.command == 'seal':
            plan = load(args.plan)
            # Edits are allowed, but must be checked against the current visible source rows.
            rows, _ = flags(dict(config, review=dict(config['review'], max_flags=sys.maxsize)), state_load(home))
            by_id = {r['flag_id']: r for r in rows}
            state = state_load(home)
            if any(i['write_id'] in state['writes'] for i in plan['items']):
                raise Error('A write was attempted from this review; prepare a new review of remaining flags.')
            for item in plan['items']:
                if any(by_id.get(s['flag_id'], {}).get('fingerprint') != s['fingerprint'] for s in item['sources']):
                    raise Error('Edited review references changed/handled source flags; review again.')
                item['sources'] = [{k: by_id[i][k] for k in (
                    'flag_id', 'fingerprint', 'kind', 'lane', 'app', 'domain', 'created_at')}
                    for i in item['flag_ids']]
                item['write_id'] = str(uuid.uuid4())
            validate_plan(plan)
            native = Native(config) if any(i['recommendation'] == 'include' for i in plan['items']) else None
            try:
                if native:
                    native.start('Read fresh destinations and revision guards for an edited local Goldfish proposal.')
                    for item in plan['items']:
                        if item['recommendation'] == 'include':
                            resolve(native, config, item)
            finally:
                if native:
                    native.close()
            plan['digest'] = digest({k: v for k, v in plan.items() if k != 'digest'})
            write(args.plan, plan)
            show(plan, config); print('New approval digest: %s' % plan['digest'])
        elif args.command == 'decide':
            state = state_load(home)
            rows, _ = flags(dict(config, review=dict(config['review'], max_flags=sys.maxsize)), state)
            by_id = {r['flag_id']: r for r in rows}
            ids = [int(i) for i in args.flag_ids.split(',')]
            if any(i not in by_id for i in ids):
                raise Error('Choose visible, pending flags; none changed.')
            for i in ids:
                state['decisions'][str(i)] = {'status': args.decision, 'fingerprint': by_id[i]['fingerprint'],
                    'until': time.time() + args.hours * 3600 if args.decision == 'later' else 0}
            write(home / 'state.json', state)
            print('Local decision saved; nothing sent to Native.')
        elif args.command == 'briefing':
            briefing(home, config)
        elif args.command == 'undo':
            undo(home, config, args.record_id)
        elif args.command == 'schedule':
            schedule(home, config, args.action == 'enable')
        elif args.command == 'doctor':
            rows, counts = flags(config, state_load(home))
            print('Goldfish readable, read-only; %d eligible flags in next batch, %d hidden.' % (len(rows), counts['hidden_lane_count']))
            print('Claude executable: %s' % (shutil.which(config['review']['claude_command'][0]) or 'NOT FOUND'))
            context, note = native_context(config)
            print('Native context: %d records. %s' % (len(context), note))


def interrupted(signum, frame):
    raise Interrupted(signal.Signals(signum).name)


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        main()
    except (Interrupted, KeyboardInterrupt) as error:
        # Flags are only marked reviewed once their review is saved, so unfinished work is picked up next run.
        print('goldfish-review: stopped (%s). Anything not yet saved will be reviewed next time.'
              % (error or 'interrupted'), file=sys.stderr)
        sys.exit(130)
    except (Error, nc.SyncError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print('goldfish-review: %s' % error, file=sys.stderr)
        sys.exit(1)
