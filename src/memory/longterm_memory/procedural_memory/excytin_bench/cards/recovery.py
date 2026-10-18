"""Recognize narrow, evidence-triggered recovery conditions without an LLM."""
import sqlglot
from sqlglot import exp
from urllib.parse import urlsplit


SCOPES = {"SigninLogs": ("TimeGenerated", "IPAddress"),
          "EmailEvents": ("TimeGenerated", "SenderFromAddress"),
          "DeviceProcessEvents": ("Timestamp", "DeviceName")}


def time_scope(sql):
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return None
    if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1:
        return None
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].name not in SCOPES or not tree.args.get("where"):
        return None
    table = tables[0].name
    time_field, anchor_field = SCOPES[table]
    where = tree.args["where"].this
    if where.find(exp.Or) or where.find(exp.Not):
        return None
    parts = list(where.flatten()) if isinstance(where, exp.And) else [where]
    temporal, retained = [], []
    anchored = False
    for part in parts:
        fields = {c.name for c in part.find_all(exp.Column)}
        if fields == {time_field}:
            temporal.append(part.sql(dialect="mysql"))
        else:
            retained.append(part.sql(dialect="mysql"))
            if isinstance(part, exp.EQ) and isinstance(part.this, exp.Column) and part.this.name == anchor_field:
                anchored |= isinstance(part.expression, exp.Literal) and bool(part.expression.this)
    if not anchored:
        return None
    return {"table": table, "temporal": sorted(temporal), "retained": sorted(retained)}


def is_time_recovery(before, after):
    first, second = time_scope(before), time_scope(after)
    return bool(first and second and first["temporal"] and not second["temporal"]
                and first["table"] == second["table"] and first["retained"] == second["retained"])


def alert_device_scope(sql):
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return None
    tables = list(tree.find_all(exp.Table))
    if (not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1
            or len(tables) != 1 or tables[0].name != "AlertEvidence" or not tree.args.get("where")):
        return None
    where = tree.args["where"].this
    if where.find(exp.Or) or where.find(exp.Not):
        return None
    parts = list(where.flatten()) if isinstance(where, exp.And) else [where]
    device, retained, anchored = [], [], False
    for part in parts:
        if (isinstance(part, exp.EQ) and isinstance(part.this, exp.Column)
                and part.this.name == "DeviceName" and isinstance(part.expression, exp.Literal)):
            device.append(part.sql(dialect="mysql"))
        else:
            retained.append(part.sql(dialect="mysql"))
            if isinstance(part, (exp.EQ, exp.In)) and isinstance(part.this, exp.Column) and part.this.name == "AlertId":
                values = [part.expression] if isinstance(part, exp.EQ) else part.expressions
                anchored |= bool(values) and all(isinstance(v, exp.Literal) and bool(v.this) for v in values)
    return {"device": device, "retained": sorted(retained)} if anchored else None


def is_alert_device_recovery(before, after):
    first, second = alert_device_scope(before), alert_device_scope(after)
    return bool(first and second and first["device"] and not second["device"]
                and first["retained"] == second["retained"])


def _simple_select(sql):
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return None
    if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1:
        return None
    tables = list(tree.find_all(exp.Table))
    return (tree, tables[0].name) if len(tables) == 1 and tree.args.get('where') else None


def exact_string_scope(sql, table_fields):
    parsed = _simple_select(sql)
    if not parsed or parsed[1] not in table_fields:
        return None
    tree, table = parsed
    where = tree.args['where'].this
    if where.find(exp.Or) or where.find(exp.Not):
        return None
    field = table_fields[table]
    parts = list(where.flatten()) if isinstance(where, exp.And) else [where]
    matches = [part for part in parts if isinstance(part, exp.EQ) and isinstance(part.this, exp.Column)
               and part.this.name == field and isinstance(part.expression, exp.Literal) and part.expression.is_string]
    if len(matches) != 1 or not matches[0].expression.this:
        return None
    return {'table': table, 'field': field, 'value': matches[0].expression.this,
            'retained': sorted(part.sql(dialect='mysql') for part in parts if part is not matches[0])}


def url_form_scope(sql):
    scope = exact_string_scope(sql, {'DeviceNetworkEvents': 'RemoteUrl', 'UrlClickEvents': 'Url'})
    if not scope:
        return None
    try:
        host = urlsplit(scope['value'] if '://' in scope['value'] else '//' + scope['value']).hostname
    except ValueError:
        return None
    return dict(scope, hostname=host) if host and '.' in host else None


def is_url_form_recovery(before, after):
    first, parsed = url_form_scope(before), _simple_select(after)
    if not first or not parsed or first['table'] != parsed[1]:
        return False
    # Historical candidates may use a parent domain; this proves discovery
    # support only, never equivalence of URLs or membership in an incident.
    where = parsed[0].args['where'].this
    if not isinstance(where, exp.Like) or not isinstance(where.this, exp.Column) or where.this.name != first['field']:
        return False
    if not isinstance(where.expression, exp.Literal):
        return False
    needle = where.expression.this.strip('%')
    return bool('.' in needle and '%' not in needle and '_' not in needle and
                (first['hostname'] == needle or first['hostname'].endswith('.' + needle)))


def parent_role_scope(sql):
    return exact_string_scope(sql, {'DeviceProcessEvents': 'InitiatingProcessFileName'})


def is_parent_role_recovery(before, after):
    first, parsed = parent_role_scope(before), _simple_select(after)
    if not first or first['retained'] or not parsed or parsed[1] != 'DeviceProcessEvents':
        return False
    where = parsed[0].args['where'].this
    if not isinstance(where, exp.Or):
        return False
    parts = list(where.flatten())
    if len(parts) != 2 or any(not isinstance(p, exp.Like) or not isinstance(p.this, exp.Column)
                              or not isinstance(p.expression, exp.Literal) for p in parts):
        return False
    if {p.this.name for p in parts} != {'FileName', 'InitiatingProcessFileName'}:
        return False
    needles = {p.expression.this.strip('%').lower() for p in parts}
    return len(needles) == 1 and next(iter(needles)) == first['value'].rsplit('.', 1)[0].lower()
