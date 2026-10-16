"""File-backed L1/L2 storage. Online selection reads L1, then exactly one L2.

The old-shaped read model is derived only for offline builders and legacy
retrieval callers; it is not a persisted SM format.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import yaml

from . import DEFAULT_KNOWLEDGE_PATH
from .build_knowledge import (
    CATALOG_ID, DEFAULT_SOURCE, build_join_keys, load_descriptions, read_sql_schemas, stable_hash,
)

def load_annotations(tables):
    path = Path(__file__).with_name('catalog_annotations.json')
    annotations = json.loads(path.read_text(encoding='utf-8'))['tables']
    if set(annotations) != set(tables):
        raise ValueError('Reviewed L1 annotations must exactly cover queryable SQL tables')
    for name, annotation in annotations.items():
        if not annotation['desc'].strip() or not annotation['entity_fields']:
            raise ValueError(f'Missing L1 annotation: {name}')
        for entity, fields in annotation['entity_fields'].items():
            if not fields or not set(fields) <= set(tables[name]):
                raise ValueError(f'Unsupported entity annotation: {name}.{entity}')
    return annotations


def source_type_note(source_type):
    return f'源类型 {source_type}；在 ExCyTIn MySQL 中以 TEXT 存储。'


def write_knowledge(knowledge, l1_path=DEFAULT_KNOWLEDGE_PATH, *, source=DEFAULT_SOURCE):
    """Publish queryable SQL tables with actual SQL types and reviewed entities."""
    l1_path = Path(l1_path)
    l2_dir = l1_path.parent / 'l2'
    sql_schemas, incidents = read_sql_schemas(source)
    annotations = load_annotations(sql_schemas)
    source_tables = {t['name']: t for t in knowledge['tables']}
    if not set(sql_schemas) <= set(source_tables):
        raise ValueError('SQL contains tables missing from source metadata')
    cards, sources = {}, []
    for name in sorted(sql_schemas):
        table = source_tables[name]
        if set(sql_schemas[name]) != {f['name'] for f in table['field']}:
            raise ValueError(f'SQL and source columns differ: {name}')
        annotation = annotations[name]
        sources.append({'table': name, 'desc': annotation['desc'],
                        'entities': sorted(annotation['entity_fields'])})
        cards[name] = {'table_name': name, 'core_columns': [
            {'name': f['name'], 'type': sql_schemas[name][f['name']],
             'desc': source_type_note(f['data_type']) + f['description_zh']}
            for f in table['field']]}
    # Validate the complete input before publishing; remove obsolete generated cards.
    l2_dir.mkdir(parents=True, exist_ok=True)
    for name, card in cards.items():
        (l2_dir / (name + '.yaml')).write_text(
            yaml.safe_dump(card, allow_unicode=True, sort_keys=False, width=120), encoding='utf-8')
    for obsolete in l2_dir.glob('*.yaml'):
        if obsolete.stem not in cards:
            obsolete.unlink()
    l1_path.write_text(yaml.safe_dump({'log_sources': sources}, allow_unicode=True,
                                    sort_keys=False, width=120), encoding='utf-8')
    # Preserve upstream schema provenance for offline PM/EM compatibility, not online SM.
    metadata = {k: deepcopy(knowledge[k]) for k in
                ('generated_at', 'generation_policy', 'environment', 'catalog')}
    metadata['storage_format'] = 'l1-l2-v2'
    metadata['source_schemas'] = {t['name']: {f['name']: f['data_type'] for f in t['field']}
                                  for t in knowledge['tables']}
    metadata['query_catalog'] = {'table_count': len(cards),
        'field_count': sum(len(c['core_columns']) for c in cards.values()),
        'type_source': 'data/sql_files/incident_*.sql', 'incident_tables': incidents,
        'excluded_metadata_tables': sorted(set(source_tables) - set(cards))}
    (l1_path.parent / 'build_manifest.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class SemanticMemoryStore:
    def __init__(self, l1_path=DEFAULT_KNOWLEDGE_PATH):
        self.l1_path = Path(l1_path)
        self.l2_dir = self.l1_path.parent / 'l2'
        value = yaml.safe_load(self.l1_path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or set(value) != {'log_sources'} or not isinstance(value['log_sources'], list):
            raise ValueError('L1 requires log_sources')
        self._sources = {}
        for source in value['log_sources']:
            if not isinstance(source, dict) or set(source) != {'table', 'desc', 'entities'}:
                raise ValueError('L1 entries require table, desc and entities')
            name = source['table']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or name in self._sources:
                raise ValueError('L1 table names must be unique SQL identifiers')
            if not isinstance(source['desc'], str) or not source['desc'].strip():
                raise ValueError('L1 descriptions must be nonempty')
            if not isinstance(source['entities'], list) or any(not isinstance(e, str) or not e for e in source['entities']):
                raise ValueError('L1 entities must be strings')
            self._sources[name] = source

    def catalog(self):
        return {'log_sources': deepcopy(list(self._sources.values()))}

    @property
    def fingerprint(self):
        # File metadata avoids opening all L2 cards on the online path.
        files = [self.l1_path, *(self.l2_dir / f'{name}.yaml' for name in self._sources)]
        return hashlib.sha256(repr([(str(p), p.stat().st_mtime_ns, p.stat().st_size)
                                    for p in files]).encode()).hexdigest()

    def read_table(self, table_name):
        if not isinstance(table_name, str) or table_name not in self._sources:
            raise ValueError(f'Unknown L1 table: {table_name!r}')
        card = yaml.safe_load((self.l2_dir / f'{table_name}.yaml').read_text(encoding='utf-8'))
        if not isinstance(card, dict) or set(card) != {'table_name', 'core_columns'} or card['table_name'] != table_name:
            raise ValueError(f'Invalid L2 identity: {table_name}')
        columns = card['core_columns']
        if not isinstance(columns, list) or not columns:
            raise ValueError(f'L2 has no columns: {table_name}')
        seen = set()
        for col in columns:
            if not isinstance(col, dict) or set(col) != {'name', 'type', 'desc'} or any(
                not isinstance(v, str) or not v.strip() for v in col.values()):
                raise ValueError(f'Invalid L2 column: {table_name}')
            if col['name'] in seen:
                raise ValueError(f'Duplicate L2 column: {table_name}.{col["name"]}')
            seen.add(col['name'])
        return card


def load_knowledge(path=DEFAULT_KNOWLEDGE_PATH):
    """Materialize a compatibility schema for offline PM/EM builders only as needed."""
    path = Path(path)
    if path.suffix == '.json':  # Explicit legacy fixtures/imports, never the default.
        return json.loads(path.read_text(encoding='utf-8'))
    store = SemanticMemoryStore(path)
    metadata = json.loads((path.parent / 'build_manifest.json').read_text(encoding='utf-8'))
    descriptions = load_descriptions()
    tables = []
    queryable = {t['table'] for t in store.catalog()['log_sources']}
    for name, fields in metadata['source_schemas'].items():
        desc = descriptions[name]
        card = store.read_table(name) if name in queryable else None
        card_fields = {f['name']: f for f in card['core_columns']} if card else {}
        if card and set(card_fields) != set(fields):
            raise ValueError(f'L2 fields differ from source provenance: {name}')
        tables.append({'table_id': f'{CATALOG_ID}:{name}', 'name': name,
                       'description': desc['description'],
                       'description_zh': desc['description_zh'], 'field': [
                           {'field_id': f'{CATALOG_ID}:{name}:{field}', 'name': field,
                            'data_type': source_type,
                            'description_zh': card_fields[field]['desc'].removeprefix(source_type_note(source_type))
                                if card else desc['field'][field]['description_zh'],
                            'description': desc['field'][field]['description']}
                           for field, source_type in fields.items()]})
    joins = build_join_keys(tables)
    catalog = dict(metadata['catalog'])
    catalog.update(table_count=len(tables), field_count=sum(len(t['field']) for t in tables), join_key_count=len(joins))
    catalog['schema_hash'] = stable_hash({'tables': [{'name': t['name'], 'schema_hash': stable_hash(
        {f['name']: f['data_type'] for f in t['field']})} for t in tables], 'join_keys': joins})
    return {k: metadata[k] for k in ('generated_at', 'generation_policy', 'environment')} | {
        'format_version': '2.0', 'catalog': catalog, 'tables': tables, 'join_keys': joins}


def content_digest(path=DEFAULT_KNOWLEDGE_PATH):
    """Stable provenance hash covering L1 and every referenced L2 card."""
    path = Path(path)
    if path.suffix == '.json':
        return hashlib.sha256(path.read_bytes()).hexdigest()
    store = SemanticMemoryStore(path)
    payload = {'l1': store.catalog(), 'l2': [store.read_table(s['table'])
               for s in store.catalog()['log_sources']]}
    return stable_hash(payload)
