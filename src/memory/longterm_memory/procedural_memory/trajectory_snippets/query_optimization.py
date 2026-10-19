"""Derive a narrow execution view without rewriting audited training templates."""
import re

import sqlglot
from sqlglot import exp

POLICY_VERSION = 'investigation-projection-host-boundary-v1'
# Retain interpretation and correlation evidence, not full JSON payloads.
CONTEXT_FIELDS = {'AlertId', 'Title', 'EntityType', 'EvidenceRole', 'ActionType',
                  'DeviceName', 'DeviceId', 'AccountName', 'AccountUpn'}
TIME_FIELDS = ('TimeGenerated', 'Timestamp', 'EventTime', 'Time', 'ProcessCreationTime')
HOST_FIELDS = {'DeviceName', 'HostName', 'Computer', 'ComputerName', 'DestinationHostName', 'SourceHostName'}


def optimize_query(sql, *, needed_fields, dependency_outputs=()):
    tree = sqlglot.parse_one(sql, read='mysql')
    original_outputs = [p.alias_or_name for p in tree.expressions]
    host_matches = []
    # Only positive exact host predicates; preserve exclusions, wildcard searches and FQDNs.
    for predicate in list(tree.find_all(exp.EQ, exp.Like)):
        column, literal = predicate.this, predicate.expression
        if not (isinstance(column, exp.Column) and column.name in HOST_FIELDS
                and isinstance(literal, exp.Literal) and literal.is_string):
            continue
        ancestor = predicate.parent
        negated = False
        while ancestor is not None:
            negated |= isinstance(ancestor, exp.Not)
            ancestor = ancestor.parent
        name = literal.this
        if negated or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', name):
            continue
        # LIKE underscores already have wildcard semantics: leave those patterns alone.
        if isinstance(predicate, exp.Like) and '_' in name:
            continue
        escaped = name.replace('!', '!!').replace('_', '!_') + '.%'
        suffix = exp.Escape(this=exp.Like(this=column.copy(), expression=exp.Literal.string(escaped)),
                            expression=exp.Literal.string('!'))
        predicate.replace(exp.Paren(this=exp.or_(exp.EQ(this=column.copy(), expression=literal.copy()), suffix)))
        host_matches.append(column.name)

    # Narrow only plain, single-table projections. DISTINCT/grouped/compound queries
    # retain original result semantics and output contracts.
    simple = (isinstance(tree, exp.Select) and len(list(tree.find_all(exp.Select))) == 1
              and len(list(tree.find_all(exp.Table))) == 1
              and not any(tree.args.get(k) for k in ('distinct', 'group', 'having', 'joins'))
              and all(isinstance(p, exp.Column) or isinstance(p, exp.Alias) and isinstance(p.this, exp.Column)
                      for p in tree.expressions))
    if simple:
        required = set(needed_fields) | set(dependency_outputs) | CONTEXT_FIELDS
        available = {p.alias_or_name for p in tree.expressions}
        time_field = next((f for f in TIME_FIELDS if f in available), None)
        if time_field:
            required.add(time_field)
        # Keep projected filter/order keys, so an observation can be attributed to its input.
        for key in ('where', 'order'):
            clause = tree.args.get(key)
            if clause:
                required.update(c.name for c in clause.find_all(exp.Column))
        selected = [p for p in tree.expressions if p.alias_or_name in required
                    or (p.this.name if isinstance(p, exp.Alias) else p.name) in required]
        # Do not silently discard an unknown target when there is no requested field
        # or dependency in the original projection.
        if selected and (set(needed_fields) | set(dependency_outputs)) & available:
            tree.set('expressions', selected)
    outputs = [p.alias_or_name for p in tree.expressions]
    return tree.sql(dialect='mysql'), outputs, {
        'policy': POLICY_VERSION, 'original_outputs': original_outputs,
        'selected_outputs': outputs, 'host_boundary_fields': host_matches,
    }
