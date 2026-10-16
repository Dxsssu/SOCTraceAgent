"""Verify online SM against original ExCyTIn SQL, CSV headers and source metadata."""
import argparse
import csv
import io
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from .build_knowledge import DEFAULT_SOURCE, file_hash, read_schemas, read_sql_schemas
from .store import SemanticMemoryStore, load_annotations


def audit(source=DEFAULT_SOURCE):
    sql, incidents = read_sql_schemas(source)
    raw, meta_count = read_schemas(source)
    store = SemanticMemoryStore()
    catalog = {t['table']: t for t in store.catalog()['log_sources']}
    csv_fields, csv_count = {}, 0
    with ZipFile(source) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if not name.startswith('data/csv_files/') or path.suffix != '.csv' or path.name.startswith('._'):
                continue
            table = path.parent.name if len(path.parts) > 4 else path.stem
            with archive.open(name) as stream:
                header = next(csv.reader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline=''),
                                         delimiter='❖', quotechar='"'))
            csv_fields.setdefault(table, set()).update(header)
            csv_count += 1
    mismatches = []
    if set(catalog) != set(sql):
        mismatches.append({'scope': 'table_names', 'l1_only': sorted(set(catalog)-set(sql)),
                           'sql_only': sorted(set(sql)-set(catalog))})
    annotations = load_annotations(sql)
    field_count = 0
    for name, columns in sql.items():
        card = store.read_table(name)
        fields = {f['name']: f['type'] for f in card['core_columns']}
        field_count += len(fields)
        if fields != columns:
            mismatches.append({'table': name, 'scope': 'sql_fields_and_types'})
        if set(fields) != csv_fields.get(name):
            mismatches.append({'table': name, 'scope': 'csv_header'})
        if set(fields) != {f for variant in raw[name] for f in variant}:
            mismatches.append({'table': name, 'scope': 'meta_field_names'})
        if catalog[name]['entities'] != sorted(annotations[name]['entity_fields']):
            mismatches.append({'table': name, 'scope': 'entity_annotations'})
    return {
        'audit_date': '2026-10-14', 'source_sha256': file_hash(source),
        'meta_files': meta_count, 'source_tables': len(raw), 'csv_files': csv_count,
        'sql_files': len(incidents), 'sql_table_definitions': sum(map(len, incidents.values())),
        'queryable_tables': len(sql), 'queryable_fields': field_count,
        'sql_types': sorted({v for fields in sql.values() for v in fields.values()}),
        'excluded_metadata_tables': sorted(set(raw)-set(sql)),
        'mismatches': mismatches,
        'semantic_review': {
            'l1': 'All queryable tables use reviewed entity-to-column annotations, not name-substring inference.',
            'l2': 'Field coverage and SQL types fully checked. Existing descriptions retained; high-risk identity/workspace/resource distinctions spot-checked, not every description externally revalidated.',
            'references': [
                'https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/aadserviceprincipalsigninlogs',
                'https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/usage',
                'https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/heartbeat',
                'https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/azuremetrics',
                'https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/devicefilecertificateinfo',
            ],
        },
        'limits': ['Catalog is a union across incidents; a particular incident may lack tables or fields.',
                   'Compared distributed SQL and CSV headers, not a running database or all row values.',
                   'Entity tags indicate schema coverage; values and nested payloads require execution evidence.'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('audit_report.json'))
    args = parser.parse_args()
    report = audit(args.source)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('queryable_tables', 'queryable_fields', 'sql_types', 'mismatches')}))
    if report['mismatches']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
