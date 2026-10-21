"""Isolated typed path-memory prototype; not registered with the workflow."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent


class MemoryStore:
    def __init__(self, root=ROOT):
        self.sm = json.loads((root / 'semantic.json').read_text())
        self.em = json.loads((root / 'episodic.json').read_text())
        self.pm = json.loads((root / 'procedural.json').read_text())
        self.episodes = {e['id']: e for e in self.em['episodes']}
        self.validate()

    def validate(self):
        assert len(self.episodes) == len(self.em['episodes']), 'duplicate EM'
        for record in self.em['episodes'] + self.pm['procedures']:
            for spec in list(record['known'].values()) + list(record['wanted'].values()):
                assert self.sm['concepts'][spec['concept']]['kind'] == spec['kind'], 'kind mismatch'
                assert spec['cardinality'] in ('one', 'many')
        for e in self.episodes.values():
            placeholders = set(re.findall(r':(\w+)', e['sql']))
            assert placeholders == set(e['known']) | set(e.get('derived_parameters', {})), 'parameters mismatch'
            # This isolated prototype only accepts its single SELECT templates.
            assert e['sql'].lstrip().upper().startswith('SELECT ')
            assert ';' not in e['sql'] and not re.search(r'\b(INTO|OUTFILE|FOR UPDATE)\b', e['sql'], re.I)
            assert e['sql'].rstrip().endswith(f"LIMIT {e['row_limit'] + 1}"), 'missing overflow sentinel'
        for p in self.pm['procedures']:
            previous = {}
            for step in p['steps']:
                assert step['id'] not in previous, 'duplicate step'
                e = self.episodes[step['episode_id']]
                assert set(step['bindings']) == set(e['known']), 'bindings mismatch'
                for name, binding in step['bindings'].items():
                    if 'input' in binding:
                        source = p['known'][binding['input']]
                        assert source['cardinality'] == e['known'][name]['cardinality']
                    else:
                        source = previous[binding['step']]['wanted'][binding['field']]
                        assert e['known'][name]['cardinality'] == 'many', 'rows require collection input'
                    assert (source['concept'], source['kind']) == (e['known'][name]['concept'], e['known'][name]['kind']), 'edge type mismatch'
                previous[step['id']] = e
            assert p['wanted'] == e['wanted'], 'final output mismatch'

    def compile(self, episode_id, values):
        e = self.episodes[episode_id]
        if set(values) != set(e['known']):
            raise ValueError('Input names must match known fields')
        values = dict(values)
        for name, spec in e['known'].items():
            v = values[name]
            if spec['cardinality'] == 'many':
                if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                    raise ValueError('Expected nonempty string collection')
                values[name] = list(dict.fromkeys(v))
            elif not isinstance(v, str) or not v:
                raise ValueError('Expected nonempty string')
        if 'short_devices' in e.get('derived_parameters', {}):
            values['devices'] = [v.lower() for v in values['devices']]
            values['short_devices'] = [v for v in values['devices'] if '.' not in v]
        params = []
        def replace(match):
            v = values[match[1]]
            if isinstance(v, list):
                params.extend(v)
                return ','.join(['%s'] * len(v)) if v else 'NULL'
            params.append(v)
            return '%s'
        return re.sub(r':(\w+)', replace, e['sql']), tuple(params)

    def run(self, procedure_id, inputs, query):
        """query(sql, bound_params) returns dict rows. No gold data is loaded."""
        p = next(p for p in self.pm['procedures'] if p['id'] == procedure_id)
        if set(inputs) != set(p['known']):
            raise ValueError('Procedure input mismatch')
        outputs, trace = {}, []
        for step in p['steps']:
            e = self.episodes[step['episode_id']]
            values = {}
            for key, b in step['bindings'].items():
                values[key] = inputs[b['input']] if 'input' in b else list(dict.fromkeys(row[b['field']] for row in outputs[b['step']]))
            sql, params = self.compile(e['id'], values)
            entry = {'step': step['id'], 'episode_id': e['id'], 'sql': sql, 'params': params}
            trace.append(entry)
            try:
                rows = query(sql, params)
            except Exception as exc:
                entry['error'] = str(exc)
                return {'status': 'sql_error', 'trace': trace, 'candidates': []}
            entry['rows'] = rows
            status = ('empty' if not rows else 'truncated' if len(rows) > e['row_limit'] else
                      'invalid_output' if any(any(not isinstance(row.get(f), str) or not row[f] for f in e['wanted']) for row in rows) else None)
            if status:
                return {'status': status, 'trace': trace, 'candidates': []}
            outputs[step['id']] = rows
        candidates = [dict(zip(p['wanted'], v)) for v in dict.fromkeys(tuple(row[f] for f in p['wanted']) for row in rows)]
        return {'status': 'candidate_ready', 'trace': trace, 'candidates': candidates}
