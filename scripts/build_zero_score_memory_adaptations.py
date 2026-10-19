"""Merge case-reviewed zero-score adaptations; keep deferred SOPs out of runtime."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.procedural_memory.flows.catalog import apply_adaptations
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2

REVIEW = ROOT / 'runtime/pm_zero220_official_20261019'
DEST = ROOT / 'src/memory/longterm_memory/procedural_memory/flows'


def read(path): return json.loads(path.read_text())

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def replace_ids(value, mapping):
    if isinstance(value, str): return mapping.get(value, value)
    if isinstance(value, list): return [replace_ids(v, mapping) for v in value]
    if isinstance(value, dict): return {k: replace_ids(v, mapping) for k,v in value.items()}
    return value


def build():
    manifest = read(REVIEW/'manifest.json')
    expected = {c['case_id'] for c in manifest['cases']}
    baseline = TrajectorySnippetStore(read(REVIEW/'baseline_catalog.json'))
    reviews=[]; deferred=[]; parts=[]
    for group in ['endpoint','identity','cloud']:
        reviews.extend(read(REVIEW/group/'reviews.json'))
        deferred.extend({'review_group':group,'design':d} for d in read(REVIEW/group/'deferred_sops.json'))
        parts.append(read(REVIEW/group/'overlay_fragment.json'))
    ids=[r['case_id'] for r in reviews]
    if len(ids)!=len(set(ids)) or set(ids)!=expected or len(ids)!=220:
        raise ValueError('incomplete_or_duplicate_zero_score_review')
    for case in manifest['cases']:
        v=case['path_validation']
        if not v['exists'] or v['given_edges']!=v['shortest_edges']:raise ValueError('unverified_official_path')
    overlay={'format_version':'evaluation-adaptations-1','provenance':'evaluation_failure_adaptation',
             'origin':'evaluation_zero_score_official_path_adaptation','source_batch':manifest['source_batch'],
             'reviewed_cases':220,'scope':manifest['selection'],'new_templates':{},'new_episodes':[],
             'new_flows':[],'guidance_patches':[],'episode_guidance_patches':[],'deferred':[]}
    for part in parts:
        for tid,t in part['new_templates'].items():
            if tid in overlay['new_templates'] and overlay['new_templates'][tid]!=t:raise ValueError('template_collision')
            overlay['new_templates'][tid]=deepcopy(t)
        for key in ['new_episodes','new_flows','guidance_patches','episode_guidance_patches']:
            overlay[key].extend(deepcopy(part.get(key,[])))
    # Two groups independently found the same predicate/parameter contract. Union
    # plain projections to preserve both evidence contracts without duplicating EM.
    merged_ids={}; projection_merge=[]
    candidates={}
    for episode in overlay['new_episodes']:
        t=overlay['new_templates'][episode['steps'][0]['template_id']]
        tree=sqlglot.parse_one(t['sql'],read='mysql')
        if not isinstance(tree,exp.Select) or tree.args.get('distinct') or tree.args.get('joins') or not all(isinstance(e,exp.Column) for e in tree.expressions):continue
        shape=tree.copy();shape.set('expressions',[])
        key=digest([shape.sql(dialect='mysql'),t['inputs']])
        candidates.setdefault(key,[]).append(episode)
    for group in candidates.values():
        if len(group)<2:continue
        owner=group[0];template=deepcopy(overlay['new_templates'][owner['steps'][0]['template_id']]);tree=sqlglot.parse_one(template['sql'],read='mysql')
        columns={e.alias_or_name:e for e in tree.expressions}
        for other in group[1:]:
            source=overlay['new_templates'][other['steps'][0]['template_id']]
            for e in sqlglot.parse_one(source['sql'],read='mysql').expressions:columns.setdefault(e.alias_or_name,e)
            for table,fields in source['schema_fields'].items():template['schema_fields'][table]=sorted(set(template['schema_fields'].get(table,[]))|set(fields))
            owner['steps'][0]['outputs'].update(other['steps'][0]['outputs'])
            for key in ['caution','source_cases','intent_aliases','candidate_output_concepts','trigger_aliases','intent_variants']:
                values=owner.setdefault(key,[])
                for value in other.get(key,[]):
                    if value not in values:values.append(value)
            if other['trigger_entity'] not in owner.setdefault('trigger_aliases',[]):owner['trigger_aliases'].append(other['trigger_entity'])
            merged_ids[other['id']]=owner['id']
        tree.set('expressions',list(columns.values()));template['sql']=tree.sql(dialect='mysql');template['outputs']=list(columns)
        template.pop('id',None);new_tid='sql_'+digest(template);template['id']=new_tid
        overlay['new_templates'][new_tid]=template
        for episode in group:
            merged_ids[episode['steps'][0]['template_id']]=new_tid
        projection_merge.append({'episodes':[e['id'] for e in group],'retained_episode':owner['id'],'template':new_tid,'columns':list(columns)})
    overlay['new_episodes']=[e for e in overlay['new_episodes'] if e['id'] not in merged_ids]
    overlay=replace_ids(overlay,merged_ids)
    used={st['template_id'] for e in overlay['new_episodes']+overlay['new_flows'] for st in e['steps']}
    overlay['new_templates']={k:v for k,v in overlay['new_templates'].items() if k in used}
    reviews=replace_ids(reviews,merged_ids)
    deferred=replace_ids(deferred,merged_ids)
    # Verify source provenance and executable contracts before writing anything live.
    for item in overlay['new_episodes']+overlay['new_flows']+overlay['episode_guidance_patches']+overlay['guidance_patches']:
        if not item.get('source_cases') or not set(item['source_cases'])<=expected:raise ValueError('invalid_case_provenance')
    result=apply_adaptations(baseline,overlay)
    semantic=SemanticMemoryV2.load()
    physical={(r['table'],r['field']) for c in semantic.concepts.values() for r in c['fields']}
    missing={(table,f) for t in overlay['new_templates'].values() for table,fs in t['schema_fields'].items() for f in fs}-physical
    if missing:raise ValueError('missing_semantic_fields:'+str(sorted(missing)))
    for f in overlay['new_flows']:
        last=result.templates[f['steps'][-1]['template_id']]
        for contract in f.get('evidence_outputs',[]):
            if contract['column'] not in last['outputs']:raise ValueError('evidence_output_missing')
    audit={'reviewed_cases':len(reviews),'official_shortest_paths_verified':220,'new_episodes':len(overlay['new_episodes']),
           'new_flows':len(overlay['new_flows']),'episode_guidance_patches':len(overlay['episode_guidance_patches']),
           'flow_guidance_patches':len(overlay['guidance_patches']),'deferred_design_records':len(deferred),
           'projection_merges':projection_merge,'baseline_catalog_id':baseline.document['catalog_id'],
           'overlay_digest':digest(overlay),'independent_evaluation':False,'llm_accuracy_retest':False,
           'runtime_episodes':sum(len(s['steps'])==1 for s in result.snippets.values()),
           'runtime_enabled_flows':sum(len(s['steps'])>1 and s['id'] not in set(result.document.get('disabled_procedural_ids',[])) for s in result.snippets.values())}
    save(REVIEW/'merged_overlay.json',overlay);save(REVIEW/'merge_audit.json',audit)
    save(REVIEW/'merged_reviews.json',sorted(reviews,key=lambda r:r['case_id']))
    save(REVIEW/'merged_deferred_sops.json',deferred)
    print(json.dumps(audit,ensure_ascii=False))
    return overlay,audit

if __name__=='__main__':build()
