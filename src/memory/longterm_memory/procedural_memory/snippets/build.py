"""Compile reviewed intents from aligned train SQL; publish in batches of ten."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from sqlglot import exp

from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    DEFAULT_QUESTIONS_DIR, DEFAULT_SEMANTIC_KNOWLEDGE, DEFAULT_TRAJECTORIES,
    build_knowledge as build_history, load_jsonl, load_train_questions, map_trajectories_to_train,
    load_semantic_knowledge,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.mining import records_from_training
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import normalize_record
from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import retrieval_view
from .definitions import RULES, output_contract
from .store import ENVIRONMENT, PACKAGE, SnippetMemory, digest, validate_template


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def training_records():
    semantic = retrieval_view(load_semantic_knowledge(DEFAULT_SEMANTIC_KNOWLEDGE))
    schema = {t['name']: t for t in semantic['tables']}
    questions = load_train_questions(DEFAULT_QUESTIONS_DIR)
    mapped = map_trajectories_to_train(load_jsonl(DEFAULT_TRAJECTORIES), questions)
    history = build_history(trajectories_path=DEFAULT_TRAJECTORIES,
                            questions_dir=DEFAULT_QUESTIONS_DIR, semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE)
    records = records_from_training(history['episodes'], mapped, semantic)
    manifest = {'split': 'train', 'question_count': len(questions), 'aligned_episodes': len(mapped),
                'sql_attempts': len(records), 'trajectory_sha256': file_digest(DEFAULT_TRAJECTORIES),
                'question_sha256': {p.name: file_digest(p) for p in sorted(DEFAULT_QUESTIONS_DIR.glob('*.json'))},
                'schema_source_sha256': file_digest(DEFAULT_SEMANTIC_KNOWLEDGE),
                'alignment': 'initial user prompt exact question match; not unreliable top-level trajectory key'}
    return records, schema, manifest


def build_rule(rule, records, schema):
    grouped = {}
    rejected = Counter()
    table, field = rule['table'], rule['field']
    columns = {f['name']: f for f in schema[table]['fields']}
    id_to_name = {f['field_id']: f['name'] for f in columns.values()}
    for record in records:
        if record['table'] != table or record['fields'] != [field]:
            continue
        available = {id_to_name[f] for f in record['output_fields'] if f in id_to_name}
        if not set(rule['outputs']) <= available:
            rejected['requested_outputs_not_observed'] += 1
            continue
        try:
            _, tree, inputs, bindings, _ = normalize_record(record, schema)
        except ValueError as exc:
            rejected[str(exc)] += 1
            continue
        if len(inputs) != 1:
            rejected['not_atomic_input'] += 1
            continue
        key, spec = next(iter(inputs.items()))
        if spec['operator'] not in {'EQ', 'IN', 'LIKE'}:
            rejected['unsupported_comparison'] += 1
            continue
        outputs = list(dict.fromkeys([field, *rule['outputs']]))
        tree.set('expressions', [exp.column(name) for name in outputs])
        tree.set('limit', None)
        tree = tree.limit(100)
        for placeholder in tree.find_all(exp.Placeholder):
            placeholder.set('this', 'value')
        sql = tree.sql(dialect='mysql')
        ident = 'tpl_' + digest([sql, spec['type']])
        group = grouped.setdefault(ident, {'tree': tree, 'spec': spec, 'outputs': outputs, 'support': []})
        group['support'].append({'episode_id': record['episode_id'], 'incident': record['incident'],
                                 'attempt_id': record['attempt_id']})
    templates, evidence = [], {}
    for ident, group in sorted(grouped.items()):
        refs = group['support']
        episodes = len({r['episode_id'] for r in refs})
        incidents = len({r['incident'] for r in refs})
        spec = {**group['spec'], 'source': 'trigger.value',
                'binding_note': 'LIKE requires an explicit SQL pattern; do not silently add wildcards' if group['spec']['operator'] == 'LIKE'
                                else 'IN requires a list; a single entity may be wrapped as a one-item list' if group['spec']['operator'] == 'IN'
                                else 'Use the observed entity value exactly'}
        template = {'id': ident, 'status': 'active' if episodes >= 2 and incidents >= 2 else 'candidate',
                    'table': table, 'inputs': {'value': spec}, 'sql': group['tree'].sql(dialect='mysql'),
                    'outputs': group['outputs'], 'schema_fields': sorted({c.name for c in group['tree'].find_all(exp.Column)})}
        validate_template(template, {table: columns})
        templates.append(template)
        evidence[ident] = {'episodes': episodes, 'incidents': incidents, 'support': refs,
                          'correlation_field_added': field, 'output_policy': 'all requested outputs observed in each support SQL; filter field added for correlation'}
    if not templates:
        raise ValueError(f"No successful train evidence supports {rule['id']}: {dict(rejected)}")
    operators = sorted({t['inputs']['value']['operator'] for t in templates})
    snippet = {'id': 'proc_' + rule['id'], 'version': 1,
               'status': 'active' if any(t['status'] == 'active' for t in templates) else 'candidate',
               'trigger_entity': rule['trigger_entity'], 'intent': rule['intent'], 'intent_aliases': rule['intent_aliases'],
               'applicability': {'environment': ENVIRONMENT, 'required_inputs': ['value'],
                                 'identifier_kind': rule['trigger_entity']['kind'], 'target_schema_check': 'required',
                                 'scope': 'candidate_discovery; verify incident relevance after query'},
               'steps': [{'id': 'lookup', 'depends_on': [],
                          'planner_guide': f"在 {table} 按 {field} 查询{'、'.join(rule['outputs'])}，保留 {field} 作为关联键。选用输入形式相符的模板；缺少输入先获取实体。",
                          'outputs': {name: output_contract(table, name, columns[name].get('description_zh') or columns[name].get('description') or name) for name in rule['outputs']},
                          'executor_sql_templates': templates}],
               'caution': [rule['caution'], '最多返回 100 行；达到上限时结果可能不完整，需按已知时间或实体缩小范围。',
                           '空结果不证明实体不存在；核对标识类型、数据覆盖和参数后回退 Semantic Memory。',
                           '多条结果保留为候选，不自动取第一条；SQL 成功不等于调查结论正确。'],
               'fallback': {'no_match': 'semantic', 'missing_input': 'planner_acquire_input',
                            'schema_mismatch': 'semantic', 'empty': 'semantic', 'sql_error': 'semantic',
                            'ambiguous_rows': 'planner_disambiguate', 'row_cap_reached': 'planner_refine_scope'}}
    return snippet, {'templates': evidence, 'rejected': dict(rejected), 'operators': operators}


def build(through, output):
    records, schema, manifest = training_records()
    snippets, audits = [], {}
    for rule in RULES[:through]:
        snippet, audit = build_rule(rule, records, schema)
        snippets.append(snippet)
        audits[snippet['id']] = audit
    scoped_schema = {}
    for snippet in snippets:
        for template in snippet['steps'][0]['executor_sql_templates']:
            table = template['table']
            fields = {f['name']: f for f in schema[table]['fields']}
            for field in template['schema_fields']:
                scoped_schema.setdefault(table, {})[field] = fields[field].get('type', fields[field].get('data_type', 'unknown'))
    document = {'format_version': 'investigation-snippets-1', 'environment_id': ENVIRONMENT,
                'catalog_id': digest([ENVIRONMENT, scoped_schema, snippets]), 'schema_snapshot': scoped_schema, 'snippets': snippets}
    SnippetMemory(document)
    audit = {'source': manifest, 'definition_sha256': file_digest(Path(__file__).with_name('definitions.py')),
             'snippets': audits, 'counts': {'snippets': len(snippets), 'active': sum(s['status'] == 'active' for s in snippets),
                                           'candidate': sum(s['status'] == 'candidate' for s in snippets),
                                           'templates': sum(len(s['steps'][0]['executor_sql_templates']) for s in snippets)},
             'policy': {'contains_test_data': False, 'contains_case_values_in_runtime': False,
                        'candidate_default_enabled': False, 'active_min_episodes': 2, 'active_min_incidents': 2,
                        'training_answer_used_for_selection': False, 'success_definition': 'source SQL returned rows; not end-to-end correctness'}}
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in [('snippets.json', document), ('build_audit.json', audit)]:
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 训练集调查片段目录', '',
             '状态按 SQL 模板的训练支持判定；active 片段仅开放其中的 active 模板。candidate 默认不进入快通道。', '',
             '| # | ID | 起点类型 / kind | 调查意图 | 表 | 状态 | 模板数 | 支持题目 / 事件 |',
             '|---|---|---|---|---|---|---:|---:|']
    for index, snippet in enumerate(snippets, 1):
        support = [r for t in audits[snippet['id']]['templates'].values() for r in t['support']]
        trigger = snippet['trigger_entity']
        templates = snippet['steps'][0]['executor_sql_templates']
        lines.append(f"| {index} | `{snippet['id']}` | {trigger['concept']} / {trigger['kind']} | {snippet['intent']} | {templates[0]['table']} | {snippet['status']} | {len(templates)} | {len({r['episode_id'] for r in support})} / {len({r['incident'] for r in support})} |")
    (output.parent / 'catalog.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'built_through': through, **audit['counts'], 'source': {k: manifest[k] for k in ['question_count','aligned_episodes','sql_attempts']}}, ensure_ascii=False), flush=True)
    return document, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through', type=int, default=len(RULES))
    parser.add_argument('--output', type=Path, default=PACKAGE / 'excytin')
    args = parser.parse_args()
    if not 1 <= args.through <= len(RULES):
        parser.error('--through outside reviewed rule range')
    build(args.through, args.output)


if __name__ == '__main__':
    main()
