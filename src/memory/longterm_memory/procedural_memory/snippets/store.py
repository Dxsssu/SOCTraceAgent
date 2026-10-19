"""Small local snippet store. It does not activate or modify the investigation workflow."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

import sqlglot
from sqlglot import exp

PACKAGE = Path(__file__).parent
ENVIRONMENT = 'excytin-azure-mysql'
STRICT_KINDS = {'Email_ID', 'Device_ID', 'Account_ID', 'File_Hash'}
ALIASES = {'邮件ID': 'Email_ID', '邮件标识': 'Email_ID', 'IP地址': 'IP', 'Source_IP': 'IP',
           'Login_IP': 'IP', 'Remote_IP': 'IP', '网址': 'URL', '链接': 'URL', '文件名': 'File_Name',
           '主机名': 'Device_Name', '设备ID': 'Device_ID', '告警ID': 'Alert_ID', '命令行': 'Process_Command',
           'PID': 'Process_ID', '账户UPN': 'Account_UPN', '发件人': 'Email_Sender', '收件人': 'Email_Recipient'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def normalized(value):
    return re.sub(r'[\s_-]+', '', value).casefold()


def validate_template(template, schema):
    trees = sqlglot.parse(template['sql'], read='mysql')
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError('Exactly one SELECT is required')
    tree = trees[0]
    if tree.args.get('into') or tree.args.get('locks') or tree.args.get('joins') or tree.find(exp.Subquery) or tree.args.get('with_'):
        raise ValueError('Only a single read-only table lookup is supported')
    refs = list(tree.find_all(exp.Table))
    if len(refs) != 1 or refs[0].name != template['table'] or refs[0].db or refs[0].catalog:
        raise ValueError('Unexpected template table/database')
    table = refs[0].name
    if table not in schema:
        raise ValueError('Schema table missing: ' + table)
    fields = {c.name for c in tree.find_all(exp.Column)}
    if not fields <= set(schema[table]) or tree.find(exp.Star):
        raise ValueError('Schema columns missing or wildcard present')
    if any(c.table and c.table != refs[0].alias_or_name for c in tree.find_all(exp.Column)):
        raise ValueError('Foreign column qualifier')
    if any(not isinstance(c, exp.Column) for c in tree.expressions):
        raise ValueError('Only explicit column projections are supported')
    if [c.name for c in tree.expressions] != template['outputs']:
        raise ValueError('Output contract differs from SQL')
    if fields != set(template['schema_fields']):
        raise ValueError('Schema dependency contract differs from SQL')
    placeholders = list(tree.find_all(exp.Placeholder))
    if len(placeholders) != 1 or placeholders[0].name != 'value' or set(template['inputs']) != {'value'}:
        raise ValueError('A single typed value input is required')
    spec = template['inputs']['value']
    where = tree.args.get('where')
    if where is None:
        raise ValueError('Unscoped query')
    predicate = where.this
    while isinstance(predicate, exp.Paren):
        predicate = predicate.this
    classes = {'EQ': exp.EQ, 'IN': exp.In, 'LIKE': exp.Like}
    if type(predicate) is not classes.get(spec['operator']):
        raise ValueError('Filter operator differs from input contract')
    if not isinstance(predicate.this, exp.Column) or predicate.this.name != spec['field']:
        raise ValueError('Filter field differs from input contract')
    values = predicate.expressions if isinstance(predicate, exp.In) else [predicate.expression]
    if len(values) != 1 or not isinstance(values[0], exp.Placeholder):
        raise ValueError('Unexpected predicate expression')
    if (spec['operator'] == 'IN') != spec['type'].endswith('[]'):
        raise ValueError('List parameter contract differs from IN')
    if spec['type'] not in {'string', 'number', 'string[]', 'number[]'}:
        raise ValueError('Unsupported parameter type')
    limit = tree.args.get('limit')
    if limit is None or not isinstance(limit.expression, exp.Literal) or limit.expression.this != '100':
        raise ValueError('Row cap must be 100')
    if any(literal.parent is not limit for literal in tree.find_all(exp.Literal)):
        raise ValueError('Case literal embedded in template')
    allowed = {'expressions', 'from_', 'where', 'limit', 'order', 'distinct'}
    if any(value and key not in allowed for key, value in tree.args.items()):
        raise ValueError('Unsupported SELECT clause')
    if tree.find(exp.Func):
        raise ValueError('Functions are not permitted in these lookup snippets')
    return tree


def compile_query(template, bindings, schema):
    """Return MySQL connector SQL plus separate values; never interpolate values."""
    tree = validate_template(template, schema).copy()
    if not isinstance(bindings, dict) or set(bindings) != {'value'}:
        raise ValueError('Bind exactly the required input: value')
    spec = template['inputs']['value']
    value = bindings['value']
    kind = spec['type'].removesuffix('[]')
    is_list = spec['type'].endswith('[]')
    values = value if is_list else [value]
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValueError('List input requires 1–50 values')
    for item in values:
        if kind == 'string':
            if not isinstance(item, str) or not item.strip() or len(item) > 4000:
                raise ValueError('Expected a non-empty string up to 4000 characters')
        elif type(item) not in (int, float) or not math.isfinite(item):
            raise ValueError('Expected a finite number')
    parameter = next(tree.find_all(exp.Placeholder))
    if is_list:
        parameter.parent.set('expressions', [exp.Var(this='%s') for _ in values])
    else:
        parameter.replace(exp.Var(this='%s'))
    return tree.sql(dialect='mysql'), tuple(values)


class SnippetMemory:
    def __init__(self, document):
        if document['format_version'] != 'investigation-snippets-1':
            raise ValueError('Unsupported snippet format')
        if document['catalog_id'] != digest([document['environment_id'], document['schema_snapshot'], document['snippets']]):
            raise ValueError('Snippet catalog digest mismatch')
        self.document = deepcopy(document)
        self.snippets = {s['id']: s for s in self.document['snippets']}
        if len(self.snippets) != len(document['snippets']):
            raise ValueError('Duplicate snippet id')
        for snippet in self.snippets.values():
            # Training construction currently emits atomic snippets. Multi-hop plans
            # compose them after new evidence creates the next typed E/F entity.
            if len(snippet['steps']) != 1:
                raise ValueError('This catalog supports atomic snippets only')
            for template in snippet['steps'][0]['executor_sql_templates']:
                validate_template(template, document['schema_snapshot'])

    @classmethod
    def load(cls, path=PACKAGE / 'excytin' / 'snippets.json'):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def retrieve(self, *, trigger_entity, intent, kind=None, schema=None,
                 environment=ENVIRONMENT, include_candidates=False):
        concept = ALIASES.get(trigger_entity, trigger_entity)
        if environment != self.document['environment_id']:
            return {'route': 'semantic', 'reason': 'environment_mismatch', 'candidates': []}
        matches, blocked = [], []
        for snippet in self.snippets.values():
            if snippet['trigger_entity']['concept'] != concept:
                continue
            # Exact intent/alias matching is deliberately conservative: a sender
            # keyword inside an unrelated DLP question must not authorize a template.
            intents = [snippet['id'], snippet['intent'], *snippet['intent_aliases']]
            if normalized(intent) not in {normalized(v) for v in intents}:
                continue
            expected_kind = snippet['trigger_entity']['kind']
            if concept in STRICT_KINDS and kind != expected_kind:
                blocked.append({'id': snippet['id'], 'reason': 'identifier_kind_required', 'expected_kind': expected_kind})
                continue
            variants = []
            for template in snippet['steps'][0]['executor_sql_templates']:
                if template['status'] != 'active' and not include_candidates:
                    continue
                try:
                    validate_template(template, schema if schema is not None else self.document['schema_snapshot'])
                except ValueError:
                    continue
                variants.append({'id': template['id'], 'inputs': template['inputs'], 'status': template['status']})
            if variants:
                matches.append({'id': snippet['id'], 'intent': snippet['intent'],
                                'trigger_entity': snippet['trigger_entity'],
                                'planner_guide': snippet['steps'][0]['planner_guide'],
                                'outputs': snippet['steps'][0]['outputs'],
                                'templates': variants, 'caution': snippet['caution'],
                                'readiness': 'schema_checked' if schema is not None else 'needs_live_schema'})
        return {'route': 'procedural' if matches else 'semantic',
                'reason': 'matched_intent' if matches else 'no_applicable_snippet',
                'candidates': matches, 'blocked': blocked}

    def prepare(self, *, snippet_id, template_id, bindings, schema, kind=None,
                environment=ENVIRONMENT, include_candidates=False):
        snippet = self.snippets[snippet_id]
        if environment != self.document['environment_id']:
            raise ValueError('Environment mismatch')
        if snippet['trigger_entity']['concept'] in STRICT_KINDS and kind != snippet['trigger_entity']['kind']:
            raise ValueError('Identifier kind mismatch')
        if schema is None:
            raise ValueError('Live target schema is required before execution')
        templates = snippet['steps'][0]['executor_sql_templates']
        template = next((t for t in templates if t['id'] == template_id), None)
        if template is None or (template['status'] != 'active' and not include_candidates):
            raise ValueError('Template not eligible in this snippet')
        sql, params = compile_query(template, bindings, schema)
        return {'snippet_id': snippet_id, 'template_id': template_id, 'sql': sql, 'params': params,
                'outputs': template['outputs'], 'caution': snippet['caution'],
                'on_empty': 'semantic', 'on_error': 'semantic', 'row_cap': 100}

    def prepare_batches(self, **kwargs):
        """Plan bounded IN batches without dropping values or silently truncating inputs.

        Callers execute every batch, preserve entity-to-row associations, and treat
        any row-cap hit or batch error as an incomplete result, not a complete union.
        """
        bindings = kwargs.get('bindings')
        if not isinstance(bindings, dict) or set(bindings) != {'value'}:
            raise ValueError('Bind exactly the required input: value')
        value = bindings['value']
        if not isinstance(value, list):
            return [self.prepare(**kwargs)]
        if not 1 <= len(value) <= 1000:
            raise ValueError('Batch input requires 1–1000 values; otherwise refine the task')
        # Validate every chunk before the caller can execute the first one.
        return [self.prepare(**{**kwargs, 'bindings': {'value': value[i:i + 50]}})
                for i in range(0, len(value), 50)]
