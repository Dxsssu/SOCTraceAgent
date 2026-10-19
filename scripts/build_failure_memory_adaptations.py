"""Assemble reviewed evaluation adaptations without altering training catalogs."""
import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory.longterm_memory.procedural_memory.flows.catalog import PACKAGE, EPISODIC, compose_catalog, apply_adaptations
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2


def read(path):
    return json.loads(path.read_text())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def build(review_dir, destination):
    manifest = read(review_dir / 'manifest.json')
    expected = {x['case_id'] for x in manifest['cases']}
    reviews, fragments, validations = {}, [], {}
    for group in sorted(review_dir.glob('review_*')):
        source = group / 'reviews.json'
        records = read(source) if source.exists() else [read(p) for p in sorted(group.glob('incident_*.json'))]
        for record in records:
            cid = record['case_id']
            if cid in reviews:
                raise ValueError(f'duplicate_case_review:{cid}')
            packet = read(review_dir / 'packets' / f'{cid}.json')
            if record['source_sha256'] != packet['source_sha256']:
                raise ValueError(f'case_source_mismatch:{cid}')
            sql_steps = [s['step_no'] for s in packet['process'] if s['action_type'] == 'query']
            reviewed = record.get('reviewed_evidence_step_ids', record.get('reviewed_step_ids', []))
            if not set(sql_steps) <= set(reviewed):
                raise ValueError(f'incomplete_sql_sequence_review:{cid}')
            record['sql_step_ids'] = sql_steps
            reviews[cid] = record
        fragment = read(group / 'overlay_fragment.json')
        assert fragment['provenance'] == 'evaluation_failure_adaptation'
        fragments.append(fragment)
        validation = group / 'overlay_validation.json'
        if validation.exists():
            validations[group.name] = read(validation)
    if set(reviews) != expected:
        raise ValueError(f'incomplete_review:{sorted(expected-set(reviews))}; extra:{sorted(set(reviews)-expected)}')
    result = {'format_version': 'evaluation-adaptations-1',
              'provenance': 'evaluation_failure_adaptation',
              'source_batch': 'deepseek_all_25', 'reviewed_cases': len(reviews),
              'scope': manifest['scope'], 'new_templates': {}, 'new_episodes': [],
              'new_flows': [], 'guidance_patches': [], 'deferred': []}
    patches = defaultdict(list)
    for part in fragments:
        for tid, template in part['new_templates'].items():
            if tid in result['new_templates'] and result['new_templates'][tid] != template:
                raise ValueError(f'template_collision:{tid}')
            result['new_templates'][tid] = template
        for key in ('new_episodes', 'new_flows', 'deferred'):
            result[key].extend(deepcopy(part[key]))
        for patch in part['guidance_patches']:
            patches[patch['flow_id']].append(patch)
    for fid, parts in sorted(patches.items()):
        guides = defaultdict(list)
        for part in parts:
            for step, guide in part['step_guides'].items():
                if guide not in guides[step]:
                    guides[step].append(guide)
        result['guidance_patches'].append({
            'id': 'patch_eval_' + digest([p['id'] for p in parts]), 'flow_id': fid,
            'source_patch_ids': [p['id'] for p in parts],
            'source_cases': sorted({c for p in parts for c in p['source_cases']}),
            'step_guides': {s:'\n'.join(gs) for s,gs in guides.items()},
            'caution_add': list(dict.fromkeys(c for p in parts for c in p.get('caution_add', [])))})
    for item in result['new_flows'] + result['guidance_patches']:
        if not set(item['source_cases']) <= expected:
            raise ValueError('unknown_adaptation_source_case')
    base = compose_catalog(read(PACKAGE / 'catalog.json'), read(EPISODIC))
    disabled = set(read(PACKAGE / 'activation.json')['disabled_flow_ids'])
    if disabled & patches.keys():
        raise ValueError('cannot_patch_suspended_flow')
    # Identical SQL chains are not new workflows; preserve dormant historical flows.
    signatures = {tuple(s['template_id'] for s in f['steps']): f['id']
                  for f in base.snippets.values() if len(f['steps']) > 1}
    for flow in result['new_flows']:
        sig = tuple(s['template_id'] for s in flow['steps'])
        if sig in signatures:
            raise ValueError(f'duplicate_flow_chain:{flow["id"]}:{signatures[sig]}')
        signatures[sig] = flow['id']
    replayed = {chain['flow_id'] for report in validations.values()
                for chain in (report['chains'] if isinstance(report, dict) else report)}
    if {f['id'] for f in result['new_flows']} - replayed:
        raise ValueError('missing_new_flow_replay_report')
    updated = apply_adaptations(base, result)
    semantic = SemanticMemoryV2.load()
    semantic_fields = {(r['table'], r['field']) for c in semantic.concepts.values() for r in c['fields']}
    missing_fields = {(t, f) for q in updated.templates.values()
                      for t, fs in q['schema_fields'].items() for f in fs} - semantic_fields
    if missing_fields:
        raise ValueError(f'runtime_templates_missing_semantic_fields:{sorted(missing_fields)}')
    audit = {'provenance': result['provenance'], 'reviewed_cases': len(reviews),
             'scope': manifest['scope'], 'base_flows': 99, 'disabled_flows': len(disabled),
             'reviewed_sql_steps': sum(len(r['sql_step_ids']) for r in reviews.values()),
             'missing_case_reviews': [], 'missing_sql_step_reviews': [],
             'runtime_templates_missing_semantic_fields': [],
             'new_flows': len(result['new_flows']), 'adjusted_existing_flows': len(patches),
             'active_flows': len(signatures) - len(disabled),
             'episodic_snippets': sum(len(s['steps']) == 1 for s in updated.snippets.values()),
             'new_episodes': len(result['new_episodes']), 'new_sql_templates': len(result['new_templates']),
             'deferred_designs': len(result['deferred']), 'adaptations_digest': digest(result),
             'independent_evaluation': False, 'accuracy_retest_performed': False,
             'review_scope': 'All case SQL sequences and reviewer summaries, targeted original observations; not every cell of wide results read verbatim.'}
    save(destination / 'adaptations.json', result)
    save(destination / 'failure_review/cases.json', [reviews[c] for c in sorted(reviews)])
    save(destination / 'failure_review/audit.json', audit)
    save(destination / 'failure_review/sql_validation.json', validations)
    retrieval = review_dir / 'retrieval_validation.json'
    if retrieval.exists():
        save(destination / 'failure_review/retrieval_validation.json', read(retrieval))
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('review_dir', type=Path)
    parser.add_argument('--output', type=Path, default=PACKAGE)
    args = parser.parse_args()
    print(json.dumps(build(args.review_dir, args.output), ensure_ascii=False, indent=2))
