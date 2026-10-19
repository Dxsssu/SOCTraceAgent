"""Merge reviewed shortest-path memories; preserve SQL and semantic boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory.longterm_memory.procedural_memory.flows.catalog import apply_adaptations
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
import sqlglot

REVIEW = ROOT / 'runtime/pm_rebuild_regression8_20261019'

def read(path): return json.loads(path.read_text())
def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

def build():
    overlay = read(REVIEW/'identity/official_path_adaptations_v2.json')
    before = read(REVIEW/'baseline_official_overlay.json')
    frozen = read(REVIEW/'baseline_catalog.json')
    old = {s['id']:s for s in frozen['snippets']}
    overlay['existing_memory_updates'] = []
    for patch in overlay.pop('episode_metadata_patches', []):
        snippet = deepcopy(old[patch['episode_id']])
        snippet.update(patch['set'])
        overlay['existing_memory_updates'].append({'id':snippet['id'],
            'source_cases':patch['source_cases'],'reason':patch['reason'],'snippet':snippet})
    # These historical flows expose broad intermediate rows; their actual
    # investigation goals are narrower than every projected column.
    goals = {
        'pm_eval_mail_device_aad_namespace':['Field:AadDeviceId'],
        'pm_eval_mail_host_alert_entity_join':['File_Name'],
        'proc_5c7e05e93755dd149afddcf8':['Account_UPN'],
        'proc_27d833654bc9b47b340c1986':['IP'],
        'proc_fde8ebf1ace3f6fd89c19636':['Field:AccountSid'],
    }
    for sid, targets in goals.items():
        snippet = deepcopy(old[sid]); snippet['target_concepts'] = targets
        overlay['existing_memory_updates'].append({'id':sid,
            'source_cases':['incident_34-q0069','incident_34-q0052','incident_5-q0019'],
            'reason':'Distinguish the actual investigation goal from incidental intermediate SQL columns.',
            'snippet':snippet})
    endpoint = read(REVIEW/'endpoint/overlay_fragment.json')
    rule_flow = deepcopy(old['proc_87e8cf9d459f7e81ed5d7ccb'])
    rule_flow['required_intent_groups'] = [['inbox','mailbox','收件箱','邮箱规则']]
    overlay['existing_memory_updates'].append({'id':rule_flow['id'],
        'source_cases':['incident_134-q0045','incident_166-q0042'],
        'reason':'Mailbox-rule recovery applies to mailbox-rule investigations, not arbitrary account lookups.',
        'snippet':rule_flow})
    # Valid JSON rows alone must not imply complete coverage of an alert type.
    guarded = next(e for e in endpoint['new_episodes'] if e['id'] == 'em_reg8_81f5ed35986466fed29bebbc')
    old_tid = guarded['steps'][0]['template_id']
    template = endpoint['new_templates'].pop(old_tid)
    template['sql'] = template['sql'].replace(
        'i.Address FROM SecurityAlert',
        "i.Address, (SELECT COUNT(*) FROM SecurityAlert bad WHERE bad.AlertName=:p2 AND COALESCE(JSON_VALID(bad.Entities),0)=0) AS UnparsedAlertCount FROM SecurityAlert")
    template['outputs'].append('UnparsedAlertCount')
    template.pop('id',None); tid = 'sql_'+digest(template); template['id'] = tid
    endpoint['new_templates'][tid] = template
    warning = 'UnparsedAlertCount>0表示同主题仍有无法解析的告警，不能声称候选完整或唯一；空结果也不排除坏JSON中的线索。同告警主机/IP共现不等于逐项主机-IP映射。'
    for item in endpoint['new_episodes']+endpoint['new_flows']:
        for step in item['steps']:
            if step['template_id'] == old_tid:
                step['template_id'] = tid
                step['outputs']['UnparsedAlertCount'] = {'description':'同主题未解析告警数','entity_concept':None,'entity_kind':None}
                step['planner_guide'] += '\n'+warning
                item['caution'].append(warning)
    for key in ['new_episodes','new_flows','guidance_patches']:
        overlay[key].extend(endpoint.get(key, []))
    for tid, template in endpoint['new_templates'].items():
        if tid in overlay['new_templates'] and overlay['new_templates'][tid] != template:
            raise ValueError('conflicting_template')
        overlay['new_templates'][tid] = template
    explicit = {
        'pm_zero_cloud_e3bf59a587207ffa2e24909c':['IP'],
        'pm_zero_cloud_7dd30e3539857832f1fd1be4':['Field:AccountDisplayName'],
        'pm_zero_cloud_11b2391c983622244014a339':['Field:ApplicationId'],
        'pm_reg8_4208bed59ffe96401498256e':['File_Name','Process_Command'],
        'pm_reg8_78b65ce27c57ab461acba78f':['Account_Name','Field:AccountSid','Field:AccountObjectId'],
        'pm_reg8_4085532134fc2fd12ec40bf4':['IP'],
    }
    for flow in overlay['new_flows']:
        flow['target_concepts'] = explicit.get(flow['id'],[v['concept'] for v in flow.get('evidence_outputs',[])])
        if flow['id'].startswith('pm_zero_cloud_'):
            flow['required_intent_groups'] = [['sap']]
        if flow['id'] == 'pm_reg8_4208bed59ffe96401498256e':
            flow['required_intent_groups'] = [['ldap','目录查询']]
        if flow['id'] == 'pm_reg8_78b65ce27c57ab461acba78f':
            flow['required_intent_groups'] = [['impacket','双用途工具','dual-purpose','重命名工具','unexpected filename','隐藏工具']]
        if flow['id'] == 'pm_reg8_4085532134fc2fd12ec40bf4':
            flow['required_intent_groups'] = [['prt','primary refresh token','横向移动','lateral movement']]
        for variant in flow.get('intent_variants') or [flow]:
            # Stage direction matters: source SAP -> fraud is not fraud -> SAP.
            variant['goal_intent'] = variant['name'].split('→')[-1].strip()
            if '|' in variant['intent'] and '→' in variant['intent']:
                variant['goal_intent'] += ' '+variant['intent'].split('|')[-1].split('→')[-1]
    reviews = read(REVIEW/'identity/reviews.json') + read(REVIEW/'endpoint/reviews.json')
    ids = [r['case_id'] for r in reviews]
    if len(ids) != 8 or len(set(ids)) != 8: raise ValueError('review_scope_not_eight_unique_cases')
    proposal = read(REVIEW/'identity/merge_proposal.json')
    by_id = {f['id']:f for f in before['new_flows']}
    signature = lambda f: ([s['template_id'] for s in f['steps']], f['bindings'], f['trigger_entity'])
    signatures = [signature(by_id[sid]) for sid in proposal['members']]
    if any(s != signatures[0] for s in signatures): raise ValueError('unsafe_flow_family_merge')
    merged = next(f for f in overlay['new_flows'] if f['id'] == proposal['retained_id'])
    if len(merged['intent_variants']) != 4: raise ValueError('lost_stage_variants')
    # Rebuild over the original pre-220 layer, not over already-adapted memories.
    base = TrajectorySnippetStore.load(ROOT/'runtime/pm_zero220_official_20261019/baseline_catalog.json')
    result = apply_adaptations(base, overlay)
    sm = SemanticMemoryV2.load()
    physical = {(r['table'],r['field']) for c in sm.concepts.values() for r in c['fields']}
    missing = {(table,f) for t in overlay['new_templates'].values()
               for table,fields in t['schema_fields'].items() for f in fields} - physical
    if missing: raise ValueError('semantic_fields_missing:'+str(sorted(missing)))
    for f in overlay['new_flows']:
        if set(f['target_concepts']) - set(sm.concepts): raise ValueError('unknown_goal_concept')
    # Exact SQL matches should reuse existing atoms. Different predicates/joins
    # stay separate even when they operate on the same entity types.
    canonical = lambda t: sqlglot.parse_one(t['sql'], read='mysql').sql(dialect='mysql')
    known_sql = {canonical(t):tid for tid,t in base.templates.items()}
    exact_duplicates = []
    for tid,t in endpoint['new_templates'].items():
        if canonical(t) in known_sql: exact_duplicates.append((tid,known_sql[canonical(t)]))
    if exact_duplicates: raise ValueError('new_em_duplicates_existing_sql:'+str(exact_duplicates))
    overlay['rebuild'] = {'source_cases':sorted(ids),'merged_pm_ids':proposal['retire_ids'],
        'policy':'EM: same predicates, join keys and role contract; PM: same step templates, dependencies, entry and goal, with separate stage variants.',
        'new_episodes':len(endpoint['new_episodes']),'new_flows':len(endpoint['new_flows']),
        'existing_memory_updates':len(overlay['existing_memory_updates'])}
    audit = {'reviewed_cases':8,'before_active_pm':65,'after_active_pm':sum(len(s['steps'])>1 and s['id'] not in result.document['disabled_procedural_ids'] for s in result.snippets.values()),
        'before_em':335,'after_em':sum(len(s['steps'])==1 for s in result.snippets.values()),
        'new_em':len(endpoint['new_episodes']),'new_pm':len(endpoint['new_flows']),
        'merged_pm_removed':proposal['retire_ids'],'existing_em_updated':[u['id']for u in overlay['existing_memory_updates'] if len(u['snippet']['steps'])==1],
        'existing_pm_targets_corrected':[u['id']for u in overlay['existing_memory_updates'] if len(u['snippet']['steps'])>1],
        'new_pm_names':[s['name']for s in endpoint['new_flows']],
        'overlay_digest':digest(overlay),'full_llm_accuracy_retest':False,
        'limitations':['Kerberos successful logon still has two SID candidates.',
                       'Impacket flow gathers account candidates; execution identity needs further role evidence.',
                       'Official shortest graph paths are compiled to executable queries, not a proof of globally minimal SQL count.']}
    save(REVIEW/'rebuild_overlay.json',overlay);save(REVIEW/'audit.json',audit);save(REVIEW/'cases.json',reviews)
    print(json.dumps(audit,ensure_ascii=False))
    return overlay,audit

if __name__ == '__main__': build()
