"""Replay snippet templates using aligned train parameters, never test QA or answers."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from src.memory.longterm_memory.procedural_memory.snippets import SnippetMemory
from src.memory.longterm_memory.procedural_memory.snippets.build import training_records, normalize_record
from src.memory.longterm_memory.procedural_memory.snippets.store import PACKAGE


def retrieval_examples(memory):
    cases = [
        ('平台邮件 ID 查发件人', dict(trigger_entity='邮件ID', kind='network_message_id', intent='发件人'), ['proc_mail_sender']),
        ('未区分邮件 ID 类型', dict(trigger_entity='邮件ID', intent='发件人'), []),
        ('邮件头 ID 候选默认关闭', dict(trigger_entity='邮件ID', kind='internet_message_id', intent='发件人'), []),
        ('研究验证显式开启邮件头 ID 候选', dict(trigger_entity='邮件ID', kind='internet_message_id', intent='发件人', include_candidates=True), ['proc_header_mail_sender']),
        ('Source_IP 认证意图', dict(trigger_entity='Source_IP', intent='认证时间'), ['proc_ip_signins']),
        ('URL 点击调查', dict(trigger_entity='URL', intent='点击者'), ['proc_url_clickers']),
        ('URL 邮件关联', dict(trigger_entity='URL', intent='邮件标识'), ['proc_url_mail_ids']),
        ('DLP 冷门意图回退', dict(trigger_entity='邮件ID', kind='network_message_id', intent='查询 DLP 违规敏感词分类与发件人'), []),
        ('目标表字段缺失', dict(trigger_entity='邮件ID', kind='network_message_id', intent='发件人', schema={}), []),
        ('环境不符', dict(trigger_entity='邮件ID', kind='network_message_id', intent='发件人', environment='another-environment'), []),
    ]
    result = []
    for title, kwargs, expected in cases:
        view = memory.retrieve(**kwargs)
        ids = [s['id'] for s in view['candidates']]
        result.append({'title': title, 'expected': expected, 'actual': ids, 'route': view['route'], 'passed': ids == expected})
    return result


def replay(start, end, output, publish=False):
    memory = SnippetMemory.load()
    audit = json.loads((PACKAGE / 'excytin/build_audit.json').read_text())
    records, source_schema, manifest = training_records()
    if manifest != audit['source']:
        raise ValueError('Training source changed since build')
    by_id = {r['attempt_id']: r for r in records}
    config = dotenv_values(ROOT / 'docker/excytin-mysql/.env')
    connections, schemas, checks = {}, {}, []
    try:
        for snippet in list(memory.snippets.values())[start:end]:
            for template in snippet['steps'][0]['executor_sql_templates']:
                refs = audit['snippets'][snippet['id']]['templates'][template['id']]['support']
                # Prefer different incidents, then different episodes. Up to two
                # train samples per template; one-episode candidates have one.
                samples, episodes, incidents = [], set(), set()
                for ref in sorted(refs, key=lambda r: (r['incident'], r['episode_id'], r['attempt_id'])):
                    if ref['incident'] not in incidents:
                        samples.append(ref); episodes.add(ref['episode_id']); incidents.add(ref['incident'])
                    if len(samples) == 2:
                        break
                if len(samples) < 2:
                    for ref in refs:
                        if ref['episode_id'] not in episodes:
                            samples.append(ref); episodes.add(ref['episode_id'])
                        if len(samples) == 2:
                            break
                for ref in samples:
                    check = {'snippet_id': snippet['id'], 'template_id': template['id'], 'source': ref,
                             'status': template['status'], 'success': False}
                    try:
                        record = by_id[ref['attempt_id']]
                        bindings = normalize_record(record, source_schema)[3]
                        value = next(iter(bindings.values()))
                        incident = ref['incident']
                        if incident not in connections:
                            db = MySQLQueryExecutor(incident, password=config['MYSQL_ROOT_PASSWORD'])
                            connections[incident] = db
                            schemas[incident] = db.semantic_schema()
                            db.connection.rollback()
                            db.connection.start_transaction(readonly=True)
                        db = connections[incident]
                        batches = memory.prepare_batches(snippet_id=snippet['id'], template_id=template['id'],
                                                  bindings={'value': value}, schema=schemas[incident],
                                                  kind=snippet['trigger_entity']['kind'], include_candidates=True)
                        counts = []
                        for prepared in batches:
                            db.cursor.execute('EXPLAIN ' + prepared['sql'], prepared['params'])
                            db.cursor.fetchall()
                            db.cursor.execute(prepared['sql'], prepared['params'])
                            counts.append(len(db.cursor.fetchall()))
                        check.update(success=bool(sum(counts)), explain_success=True, row_count=sum(counts),
                                     query_batches=len(batches), batch_row_counts=counts,
                                     possibly_truncated=any(n == 100 for n in counts))
                    except Exception as exc:
                        # Do not print raw parameter values / returned case evidence.
                        check.update(error_type=type(exc).__name__, error_code=getattr(exc, 'errno', None))
                    checks.append(check)
            print(json.dumps({'snippet': snippet['id'], 'checks': sum(c['snippet_id'] == snippet['id'] for c in checks),
                              'passed': all(c['success'] for c in checks if c['snippet_id'] == snippet['id'])}), flush=True)
    finally:
        for db in connections.values():
            db.connection.rollback()
            db.close()
    report = {'split': 'train', 'catalog_id': memory.document['catalog_id'], 'start': start, 'end': end,
              'snippets': len(list(memory.snippets.values())[start:end]), 'checks': checks,
              'passed': sum(c['success'] for c in checks), 'total': len(checks),
              'limitations': 'Training replay verifies executable templates on observed inputs, not generalization or Planner accuracy.'}
    report['retrieval_examples'] = retrieval_examples(memory)
    report['all_retrieval_examples_passed'] = all(c['passed'] for c in report['retrieval_examples'])
    report['select_statements'] = sum(c.get('query_batches', 0) for c in checks)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if publish:
        if start != 0 or end != len(memory.snippets) or report['passed'] != report['total'] or not report['all_retrieval_examples_passed']:
            raise ValueError('Publish requires a complete passing catalog replay')
        (PACKAGE / 'excytin/validation_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['snippets','passed','total']}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--end', type=int, default=40)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/procedural_memory_v2/replay.json')
    parser.add_argument('--publish', action='store_true', help='Publish complete passing validation beside the catalog')
    args = parser.parse_args()
    report = replay(args.start, args.end, args.output, publish=args.publish)
    raise SystemExit(0 if report['passed'] == report['total'] and report['total'] and report['all_retrieval_examples_passed'] else 1)
