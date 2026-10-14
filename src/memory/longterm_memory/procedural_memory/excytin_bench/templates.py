"""Canonical query templates, parameter validation and safe literal rendering."""
import json
import math
import sqlglot
from sqlglot import exp

FORMAT_VERSION = '5.0'
CATALOG_ID = 'excytin-query-templates-v5'


def render_sql(procedure, bindings):
    expected = procedure['inputs']
    if not isinstance(bindings, dict) or set(bindings) != set(expected):
        raise ValueError('Bind exactly the template inputs; missing inputs require another task')
    tree = sqlglot.parse_one(procedure['sql_template'], read='mysql')
    def literal(value, kind):
        if kind == 'string' and isinstance(value, str) and value.strip() and len(value) <= 2000:
            return exp.Literal.string(value)
        if kind == 'number' and type(value) in (int, float) and math.isfinite(value):
            return exp.Literal.number(str(value))
        raise ValueError('Invalid template parameter type or length')
    for placeholder in list(tree.find_all(exp.Placeholder)):
        key = placeholder.name
        spec = expected[key]
        value = bindings[key]
        if spec['type'].endswith('[]'):
            if not isinstance(value, list) or not 1 <= len(value) <= 50:
                raise ValueError('List parameters require 1-50 values')
            parent = placeholder.parent
            if not isinstance(parent, exp.In):
                raise ValueError('List parameter must be in IN')
            parent.set('expressions', [literal(v, spec['type'][:-2]) for v in value])
        else:
            placeholder.replace(literal(value, spec['type']))
    if tree.find(exp.Placeholder):
        raise ValueError('Unbound parameter')
    return tree.sql(dialect='mysql')


def schema_refs(procedure):
    return procedure['semantic_refs']


def validate_template(p, tables):
    tree = sqlglot.parse_one(p['sql_template'], read='mysql')
    if not isinstance(tree, exp.Select) or tree.find(exp.Subquery) or tree.args.get('joins'):
        raise ValueError('Templates must be single-table SELECT operations')
    refs = list(tree.find_all(exp.Table))
    if len(refs) != 1 or refs[0].name not in tables:
        raise ValueError('Unknown template table')
    table = refs[0].name
    names = {c.name for c in tree.find_all(exp.Column)}
    if tree.find(exp.Star) or not names <= tables[table]:
        raise ValueError('Unknown template field or wildcard projection')
    if any(c.table and c.table != refs[0].alias_or_name for c in tree.find_all(exp.Column)):
        raise ValueError('Invalid table qualifier')
    params = {n.name for n in tree.find_all(exp.Placeholder)}
    if not params or params != set(p['inputs']):
        raise ValueError('Template inputs differ from placeholders')
    if {r['table']: set(r['fields']) for r in p['semantic_refs']} != {table: names}:
        raise ValueError('Template schema references mismatch')
    if set(p['returns']) != {c.name for c in tree.expressions}:
        raise ValueError('Template returns mismatch')
    limit = tree.args.get('limit')
    if limit is None or int(limit.expression.this) != 100:
        raise ValueError('Templates must cap returned rows at 100')
    # No case values may be embedded in a memory. Only the row cap is literal.
    if any(n.parent is not limit for n in tree.find_all(exp.Literal)):
        raise ValueError('Template contains a case-specific literal')
    for spec in p['inputs'].values():
        if spec['field'] not in tables[table] or not spec['description']:
            raise ValueError('Invalid input field')
    return p
