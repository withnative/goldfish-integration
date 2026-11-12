#!/usr/bin/env python3
"""Read-only Native tools for the Goldfish reviewer (stdio MCP). Python 3.9+, standard library only.

Claude reaches Native only through this process during a review. It exposes Native's
read operations only: the allowed set is every operation Native's own catalogue
marks as access='read', checked here in code, so flag text cannot widen it. Every query and every record ID it returns is
logged locally so the kit can show what Claude looked at and check that proposed
targets are records Claude actually saw.
"""
import argparse
import json
from pathlib import Path
import sys

import native_client as nc

BODY_LIMIT = 6000
FIELDS = ('id', 'type', 'kind', 'name', 'summary', 'home_id', 'archived')
TOOLS = [
    {'name': 'search_native',
     'description': 'Full-text search of Native records you can read. Terms are AND-ed: use 1-3 short '
                    'distinctive words (a project, product, person or organisation name), never personal details.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['query'], 'properties': {
         'query': {'type': 'string', 'maxLength': 80}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}}},
    {'name': 'read_native_record',
     'description': 'Read one Native record: name, summary, lifecycle, path and body (truncated).',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['id'],
                     'properties': {'id': {'type': 'string'}}}},
    {'name': 'list_native_children',
     'description': 'Most recently active direct children of a Native collection or record.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['id'], 'properties': {
         'id': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 60}}}},
    {'name': 'list_native_collections',
     'description': 'Live collections you can read, for choosing where a new task belongs.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}}},
    {'name': 'native_read',
     'description': "Any Native read operation, e.g. sql_read/query_sql (start with SELECT * FROM catalog_relations "
                    "LIMIT 50 to see relations: records, links, facet_values, ...), records_read/get_history, "
                    "records_read/manage_links.list, records_read/whats_changed, guidance_read/read_guide. "
                    "Use describe=true to fetch an operation's argument contract first. Write operations are refused.",
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['executor', 'operation'],
                     'properties': {'executor': {'type': 'string'}, 'operation': {'type': 'string'},
                                    'arguments': {'type': 'object'}, 'describe': {'type': 'boolean'}}}},
]


def seen_records(value, found=None):
    """Every record-like object (an id plus a type or name) anywhere in a result."""
    found = [] if found is None else found
    if isinstance(value, dict):
        if isinstance(value.get('id'), str) and ('type' in value or 'name' in value):
            found.append(value)
        for child in value.values():
            seen_records(child, found)
    elif isinstance(value, list):
        for child in value:
            seen_records(child, found)
    return found


class Reader:
    def __init__(self, config, log_path):
        self.native = nc.Native(config)
        self.log_path = log_path
        self.run_key = None
        self.readable = None

    def read_operations(self):
        if self.readable is None:
            rows = self.sql("SELECT executor, operation FROM catalog_operations WHERE access='read'", [])
            self.readable = {(r['executor'], r['operation']) for r in rows}
        return self.readable

    def call(self, executor, operation, arguments, envelope=None):
        if not self.run_key:
            boot = self.native.tool('bootstrap', {'format': 'json'})
            self.run_key = boot.get('run', {}).get('run_key')
            try:
                self._call('coordination_write', 'set_intent', {
                    'intent': 'Read existing work for a local Goldfish review; no Goldfish content is saved to Native.'})
            except nc.SyncError:
                pass  # reads still work while Native is read-only; the intent is a courtesy
        if envelope:  # call_executor: executor/operation/arguments live in the envelope itself
            envelope.update(format='json', run_key=self.run_key)
            return self.native.tool(executor, envelope)
        return self._call(executor, operation, arguments)

    def _call(self, executor, operation, arguments):
        envelope = {'operation': operation, 'arguments': arguments, 'format': 'json'}
        if self.run_key:
            envelope['run_key'] = self.run_key
        return self.native.tool(executor, envelope)

    def log(self, entry):
        with self.log_path.open('a') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def sql(self, sql, parameters):
        return self.call('sql_read', 'query_sql', {'sql': sql, 'parameters': parameters}).get('rows', [])

    def run(self, name, args):
        if name == 'search_native':
            data = self.call('records_read', 'search', {'query': args['query'], 'limit': args.get('limit', 10)})
            rows = [{k: h.get(k) for k in ('id', 'type', 'kind', 'name', 'home_id', 'snippet')}
                    for h in data.get('hits', [])]
            result = {'hits': rows, 'note': 'No hits is weak evidence; try fewer or different words.'}
        elif name == 'read_native_record':
            data = self.call('records_read', 'get_record', {'ids': [args['id']]})
            found = [r for r in data.get('records', []) if r.get('status') == 'found']
            if not found:
                result, rows = {'error': 'Not found or not readable.'}, []
            else:
                record = found[0]
                body = record.get('body') or ''
                result = {k: record.get(k) for k in FIELDS}
                result['lifecycle'] = (record.get('lifecycle_interpretation') or {}).get('value', {}).get('canonical')
                result['body'] = body[:BODY_LIMIT] + ('\n[truncated]' if len(body) > BODY_LIMIT else '')
                rows = [result]
        elif name == 'list_native_children':
            rows = self.sql('SELECT id,type,name,summary FROM records WHERE home_id=?1 AND archived=0 '
                            'ORDER BY last_activity_at DESC,id LIMIT %d' % min(int(args.get('limit', 30)), 60),
                            [args['id']])
            result = {'children': rows}
        elif name == 'native_read':
            key = (args['executor'], args['operation'])
            if key not in self.read_operations():
                raise nc.SyncError('%s/%s is not a Native read operation; only reads are available here.' % key)
            if args.get('describe'):
                result = self.native.tool('describe_operation', {'executor': key[0], 'operation': key[1]})
            else:
                # The executor goes in the envelope, not as a keyword: call()'s first parameter is also
                # named executor, and passing both raised "got multiple values for argument 'executor'".
                result = self.call('call_executor', None, None, envelope={
                    'executor': key[0], 'operation': key[1], 'arguments': args.get('arguments') or {}})
            rows = seen_records(result)
        elif name == 'list_native_collections':
            rows = self.sql("SELECT id,type,name,home_id FROM records WHERE type='Collection' AND archived=0 "
                            'ORDER BY last_activity_at DESC,id LIMIT 200', [])
            result = {'collections': rows}
        else:
            raise nc.SyncError('Unknown tool ' + name)
        self.log({'tool': name, 'arguments': args,
                  'seen': [{'id': r.get('id'), 'type': r.get('type'), 'name': r.get('name')} for r in rows if r.get('id')]})
        if len(json.dumps(result)) > 60000:
            result = {'truncated': True, 'text': json.dumps(result, ensure_ascii=False)[:60000]}
        return result

    def close(self):
        if self.run_key:
            try:
                self._call('coordination_write', 'close_run', {})
            except Exception:
                pass


def serve(reader, stdin=sys.stdin, stdout=sys.stdout):
    def reply(message_id, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': message_id}
        message.update({'error': error} if error else {'result': result})
        stdout.write(json.dumps(message) + '\n')
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, message_id = message.get('method'), message.get('id')
        if message_id is None:
            continue  # notifications need no reply
        if method == 'initialize':
            reply(message_id, {'protocolVersion': message['params'].get('protocolVersion', '2025-06-18'),
                               'capabilities': {'tools': {}},
                               'serverInfo': {'name': 'goldfish-native-reader', 'version': nc.VERSION}})
        elif method == 'tools/list':
            reply(message_id, {'tools': TOOLS})
        elif method == 'tools/call':
            params = message['params']
            try:
                text = json.dumps(reader.run(params['name'], params.get('arguments') or {}), ensure_ascii=False)
                reply(message_id, {'content': [{'type': 'text', 'text': text}]})
            except (nc.SyncError, KeyError, ValueError, TypeError) as error:
                reply(message_id, {'content': [{'type': 'text', 'text': 'Native read failed: %s' % error}],
                                   'isError': True})
        elif method == 'ping':
            reply(message_id, {})
        else:
            reply(message_id, error={'code': -32601, 'message': 'Method not found'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    nc.AUTH = home / 'native-auth.json'
    reader = Reader(json.loads((home / 'config.json').read_text()), args.log)
    try:
        serve(reader)
    finally:
        reader.close()


if __name__ == '__main__':
    main()
