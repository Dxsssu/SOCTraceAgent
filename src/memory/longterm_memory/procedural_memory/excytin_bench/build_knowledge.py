"""Mine parameterized, single-table query operations from aligned training evidence."""
import argparse
import hashlib
import json
from src.memory.longterm_memory.semantic_memory.excytin_bench.store import load_knowledge as load_semantic_knowledge, content_digest
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlglot import exp
from . import DEFAULT_KNOWLEDGE_PATH
from .templates import CATALOG_ID, validate_template
from .mining import records_from_training
from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    build_knowledge as build_training_history, DEFAULT_QUESTIONS_DIR, DEFAULT_SEMANTIC_KNOWLEDGE,
    DEFAULT_TRAJECTORIES, load_jsonl, load_train_questions, map_trajectories_to_train,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import retrieval_view

CORE_OUTPUTS = {
    'AlertEvidence': ['AlertId','Title','EntityType','EvidenceRole','AccountSid','DeviceId','DeviceName','FileName','ProcessCommandLine','NetworkMessageId','RemoteIP','RemoteUrl'],
    'AlertInfo': ['AlertId','Title','Category','AttackTechniques','Timestamp'],
    'DeviceProcessEvents': ['DeviceId','DeviceName','Timestamp','FileName','ProcessId','ProcessCommandLine','InitiatingProcessFileName','InitiatingProcessId','InitiatingProcessCommandLine','AccountSid'],
    'EmailEvents': ['NetworkMessageId','InternetMessageId','SenderFromAddress','SenderMailFromAddress','SenderIPv4','RecipientEmailAddress','Subject','Timestamp'],
    'SecurityAlert': ['SystemAlertId','AlertName','Entities','Description','TimeGenerated','ExtendedProperties'],
}

DEFAULT_EPISODIC_KNOWLEDGE = Path(__file__).resolve().parents[2] / 'episodic_memory/excytin_bench/episodic_memory.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_record(record, schema):
    """Keep predicate structure; turn only literal filter values into typed inputs."""
    if not record['valid'] or record['status'] != 'rows_returned':
        raise ValueError('not_successful_single_table')
    tree = record['tree'].copy()
    table = record['table']
    fields = {f['name'].lower(): f for f in schema[table]['fields']}
    if tree.args.get('joins') or tree.find(exp.Subquery) or tree.args.get('group') or tree.args.get('having') or tree.args.get('offset'):
        raise ValueError('complex_query')
    if not tree.args.get('where'):
        raise ValueError('unscoped_query')
    for col in tree.find_all(exp.Column):
        if col.is_star:
            continue
        if col.name.lower() not in fields:
            raise ValueError('unknown_field')
        col.set('this', exp.to_identifier(fields[col.name.lower()]['name']))
        col.set('table', None)
    tables = list(tree.find_all(exp.Table))
    tables[0].set('this', exp.to_identifier(table)); tables[0].set('alias', None)
    if tables[0].db or tables[0].catalog:
        raise ValueError('qualified_database')
    projections = []
    for item in tree.expressions:
        if isinstance(item, exp.Star) or isinstance(item, exp.Column) and item.is_star:
            # Keep the candidate output vocabulary, not full row dumps in runtime memory.
            projections.extend(f for f in CORE_OUTPUTS.get(table, [f['name'] for f in fields.values() if f.get('is_identifier') or f.get('is_time_field') or any(x in f['name'] for x in ('Name','Address','Url','IP','CommandLine'))][:10]) if f.lower() in fields)
        elif isinstance(item, exp.Column):
            projections.append(item.name)
        else:
            raise ValueError('computed_projection')
    inputs, bindings = {}, {}
    where = tree.args['where']
    for node in list(where.find_all(exp.In)):
        if not isinstance(node.this, exp.Column) or not node.expressions or not all(isinstance(v, exp.Literal) for v in node.expressions):
            raise ValueError('complex_in')
        kinds = {v.is_string for v in node.expressions}
        if len(kinds) != 1:
            raise ValueError('mixed_in_types')
        key = f'p{len(inputs)+1}'; field = fields[node.this.name.lower()]
        inputs[key] = {'field': field['name'], 'type': ('string' if True in kinds else 'number') + '[]',
                       'description': field.get('description_zh') or field.get('description') or field['name'], 'operator': 'IN'}
        bindings[key] = [v.this if v.is_string else float(v.this) for v in node.expressions]
        node.set('expressions', [exp.Placeholder(this=key)])
    for node in list(where.find_all(exp.Literal)):
        parent = node.parent
        if not isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)):
            raise ValueError('unsupported_literal_expression')
        other = parent.left if parent.right is node else parent.right
        if not isinstance(other, exp.Column):
            raise ValueError('unbound_filter')
        key = f'p{len(inputs)+1}'; field = fields[other.name.lower()]
        operator = 'LIKE' if isinstance(parent, exp.Like) else parent.key.upper()
        description = field.get('description_zh') or field.get('description') or field['name']
        if operator == 'LIKE':
            description += '；提供 SQL LIKE 模式，按需包含 % 通配符'
        inputs[key] = {'field': field['name'], 'type': 'string' if node.is_string else 'number', 'description': description, 'operator': operator}
        bindings[key] = node.this if node.is_string else float(node.this)
        node.replace(exp.Placeholder(this=key))
    if not inputs or len(inputs) > 4:
        raise ValueError('input_count')
    tree.set('limit', None)
    if tree.find(exp.Literal):
        raise ValueError('literal_outside_filters')
    tree.set('expressions', [exp.column('placeholder')])
    # Group identical predicates across output projections. Sorting is retained.
    signature = tree.sql(dialect='mysql')
    return signature, tree, inputs, bindings, projections


def build_bundle(*, semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE, episodic_path=DEFAULT_EPISODIC_KNOWLEDGE,
                 trajectories_path=DEFAULT_TRAJECTORIES, questions_dir=DEFAULT_QUESTIONS_DIR):
    semantic = retrieval_view(load_semantic_knowledge(semantic_path))
    episodic = json.loads(Path(episodic_path).read_text())
    if episodic['generation_policy']['source_split'] != 'train' or digest(trajectories_path) != episodic['catalog']['source_sha256']:
        raise ValueError('Training source differs from aligned EM source')
    mapped = map_trajectories_to_train(load_jsonl(Path(trajectories_path)), load_train_questions(Path(questions_dir)))
    history = build_training_history(trajectories_path=Path(trajectories_path), questions_dir=Path(questions_dir), semantic_path=Path(semantic_path))
    records = records_from_training(history['episodes'], mapped, semantic)
    schema = {t['name']: t for t in semantic['tables']}
    groups, rejected = {}, Counter()
    for r in records:
        try:
            signature, tree, inputs, bindings, outputs = normalize_record(r, schema)
        except ValueError as exc:
            rejected[str(exc)] += 1; continue
        group = groups.setdefault(signature, {'tree': tree, 'inputs': inputs, 'outputs': Counter(), 'records': []})
        # Explicit projections outrank wildcard-derived fields, while retaining repeated useful outputs.
        weight = 1 if r['tree'].find(exp.Star) else 10
        group['outputs'].update({f: weight for f in set(outputs)})
        group['records'].append((r, bindings))
    procedures, audit = [], {}
    tables = {t: {f['name'] for f in s['fields']} for t,s in schema.items()}
    for signature, g in sorted(groups.items()):
        support = g['records']; episodes = {r['episode_id'] for r,_ in support}; incidents = {r['incident'] for r,_ in support}
        # Single-use SQL is not reusable memory. Keep counts only in offline audit.
        if len(episodes) < 2:
            rejected['single_episode_pattern'] += 1; continue
        tree = g['tree']; table = next(tree.find_all(exp.Table)).name
        field_map = {f['name']: f for f in schema[table]['fields']}
        outputs = sorted(g['outputs'], key=lambda f: (-g['outputs'][f], f))[:12]
        tree.set('expressions', [exp.column(f) for f in outputs]); tree = tree.limit(100)
        sql = tree.sql(dialect='mysql')
        ident = 'pm_' + hashlib.sha256(sql.encode()).hexdigest()[:12]
        inputs = g['inputs']
        short = lambda s: s[:100]
        for spec in inputs.values(): spec['description'] = short(spec['description'])
        name = '按' + '、'.join(dict.fromkeys(x['field'] + ('模式' if x['operator']=='LIKE' else '') for x in inputs.values())) + '查询' + table
        p = {'id': ident, 'name': name, 'status': 'active' if len(incidents) >= 2 else 'candidate',
             'purpose': name + '，获取' + '、'.join(outputs), 'inputs': inputs, 'sql_template': sql,
             'returns': {f: short(field_map[f].get('description_zh') or field_map[f].get('description') or f) for f in outputs},
             'semantic_refs': [{'table': table, 'fields': sorted({c.name for c in tree.find_all(exp.Column)})}]}
        validate_template(p, tables)
        procedures.append(p)
        audit[ident] = {'episodes': len(episodes), 'incidents': len(incidents), 'support': [{'episode_id':r['episode_id'], 'incident':r['incident'], 'attempt_id':r['attempt_id']} for r,_ in support],
                       'validation': 'parsed_readonly_and_schema_checked; source queries returned rows',
                       'projection_policy': 'union of observed outputs; explicit projections preferred; at most 12; LIMIT 100'}
    knowledge = {'format_version':'5.0','generated_at':datetime.now(UTC).isoformat(), 'environment_id':episodic['environment_id'],
        'generation_policy':{'source_split':'train','contains_sql':True,'contains_log_values':False,'contains_answers':False,'contains_test_data':False,
                             'min_episodes':2,'min_incidents_active':2,'method':'parameterized_successful_query_patterns'},
        'semantic_catalog':{'catalog_id':semantic['catalog']['catalog_id'],'schema_hash':semantic['catalog']['schema_hash']},
        'episodic_catalog':{'episodic_catalog_id':episodic['catalog']['episodic_catalog_id'],'source_sha256':episodic['catalog']['source_sha256']},
        'catalog':{'procedural_catalog_id':CATALOG_ID,'procedure_count':len(procedures),'active_count':sum(p['status']=='active' for p in procedures),
                   'training_episode_count':len(mapped),'training_attempt_count':len(records),'semantic_snapshot_sha256':content_digest(semantic_path),'episodic_snapshot_sha256':digest(episodic_path)},
        'procedures':procedures}
    return knowledge, {'source_split':'train','source_sha256':digest(trajectories_path),'templates':audit,'rejected':dict(rejected),
                       'limitations':['Runtime SQL replay is a separate validation step.', 'Current miner supports single-table filtered SELECT; joins and computed projections are excluded.', 'Successful source queries do not guarantee correct final answers.']}


def build_knowledge(**kwargs):
    return build_bundle(**kwargs)[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DEFAULT_KNOWLEDGE_PATH);parser.add_argument('--audit-output',type=Path)
    args=parser.parse_args();k,a=build_bundle()
    args.output.write_text(json.dumps(k,ensure_ascii=False,indent=2)+'\n')
    (args.audit_output or args.output.with_name('build_audit.json')).write_text(json.dumps(a,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(k['catalog'],ensure_ascii=False))

if __name__ == '__main__':main()
