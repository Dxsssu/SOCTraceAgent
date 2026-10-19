"""Audit every source/PM/live-schema field without reading incident log rows.

.venv/bin/python scripts/audit_semantic_coverage.py --mysql-ports 3306 3311 3312
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.build import SOURCE


def audit(memory, schema):
    covered = {(r['table'], r['field']) for item in memory.concepts.values() for r in item['fields']}
    expected = {(t, f) for t, fields in schema.items() for f in fields}
    missing = sorted(expected - covered)
    tables = sorted(set(schema) - set(memory.tables))
    return {'tables': len(schema), 'fields': len(expected), 'missing_tables': tables,
            'missing_fields': missing, 'passed': not tables and not missing}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mysql-ports', nargs='*', type=int, default=[])
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/semantic_memory_v2/coverage.json')
    args = parser.parse_args()
    memory = SemanticMemoryV2.load()
    source = {p.stem: {c['name']: c['type'] for c in yaml.safe_load(p.read_text())['core_columns']}
              for p in (SOURCE / 'l2').glob('*.yaml')}
    pm = json.loads((ROOT / 'src/memory/longterm_memory/procedural_memory/trajectory_snippets/excytin/catalog.json').read_text())
    pm_schema = {}
    for template in pm['templates'].values():
        for table, fields in template['schema_fields'].items():
            pm_schema.setdefault(table, set()).update(fields)
    report = {'catalog_id': memory.catalog_id, 'source': audit(memory, source),
              'procedural': {**audit(memory, pm_schema), 'templates': len(pm['templates'])}, 'mysql': {}}
    if args.mysql_ports:
        import mysql.connector
        from dotenv import dotenv_values
        config = dotenv_values(ROOT / 'docker/excytin-mysql/.env')
        for port in args.mysql_ports:
            conn = mysql.connector.connect(host='127.0.0.1', port=port, user='root',
                password=config['MYSQL_ROOT_PASSWORD'], database='env_monitor_db', connection_timeout=5)
            try:
                cur = conn.cursor()
                cur.execute('SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()')
                schema = {}
                for table, field, dtype in cur.fetchall():
                    schema.setdefault(table, {})[field] = dtype
                report['mysql'][str(port)] = audit(memory, schema)
            finally:
                conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    if not all(x['passed'] for x in [report['source'], report['procedural'], *report['mysql'].values()]):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
