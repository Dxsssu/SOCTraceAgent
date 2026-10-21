"""Validate exported references, dependencies, deduplication and case coverage."""
import collections
import json
from .core import ROOT, digest, write_json
from .export import export


def audit():
    summary=export()
    manifest=json.loads((ROOT/'manifest.json').read_text())
    sm=json.loads((ROOT/'semantic.json').read_text())
    em=json.loads((ROOT/'episodic.json').read_text())['episodes']
    pm=json.loads((ROOT/'procedural.json').read_text())['procedures']
    index=json.loads((ROOT/'case_memory_index.json').read_text())['cases']
    episodes={e['id']:e for e in em};procedures={p['id']:p for p in pm}
    assert len(episodes)==len(em) and len(procedures)==len(pm)
    assert len({digest(e['signature']) for e in em})==len(em)
    assert len({digest(p['signature']) for p in pm})==len(pm)
    for e in em:
        assert e['id']=='em_'+digest(e['signature'])[:16]
        assert e['signature']['known'] in sm['concepts'] and e['signature']['wanted'] in sm['concepts']
        assert e['parameters']==['event_title','input_values'] and ':input_values' in e['sql']
        assert 'LTMInputEvidence' in e['sql'] and 'LTMEntityEvidence' in e['sql']
    for p in pm:
        vids={v['id'] for v in p['variants']}
        for v in p['variants']:
            for entry in v['known']['any_of']:
                seed=entry['seed_entities']['concept'];known=seed
                for pos,s in enumerate(v['steps']):
                    eid=s['episode_by_input'][seed] if pos==0 else s['episode_id']
                    e=episodes[eid]
                    assert e['signature']['known']==known
                    assert s['bindings']['event_title']=={'scenario_event_index':pos}
                    expected={'input':'seed_entities'} if pos==0 else {'step':f'T{pos}','field':'entities'}
                    assert s['bindings']['entities']==expected
                    known=e['signature']['wanted']
                assert known==v['wanted']['concept']
                assert v['wanted'] in p['wanted']['any_of']
        assert len({digest(s['events']) for s in p['scenarios']})==len(p['scenarios'])
        routes={v['id']:v for v in p['variants']}
        for s in p['scenarios']:
            assert set(s['allowed_variants'])<=vids
            assert all(e in sm['events'] for e in s['events'])
            for rid in s['allowed_variants']:
                assert len(s['events'])==len(routes[rid]['steps'])
                assert set(s['allowed_inputs'][rid])<=set(routes[rid]['steps'][0]['episode_by_input'])
    offline_pass=0;live_attempted=0;live_pass=0;exact_verified=0;failures=collections.Counter();anchors=0
    steps=collections.Counter();incidents=collections.Counter();db_incomplete=[]
    exercised_em=set();verified_pm=set();verified_routes=set()
    for item in manifest['items']:
        file=ROOT/'cases'/f"{item['id']}.json"
        if not file.exists():continue
        case=json.loads(file.read_text());assert case['id'] in index
        if all(v['passed'] for v in case['offline_validation']):offline_pass+=1
        else:raise AssertionError('Offline path mismatch')
        incidents[item['id'].split('_train_')[0]]+=1
        steps[case['interpretation']['sql_steps_per_variant']]+=1
        anchors+=any(s['value_casefold_in_input'] for s in case['interpretation']['start_alternatives'])
        ref=index[case['id']]
        if ref['kind']=='PM':
            p=procedures[ref['pm']]
            assert any(s['id']==ref['scenario'] and case['id'] in s['source_refs'] for s in p['scenarios'])
        else:assert all(e in episodes for e in ref['episodes'])
        if case['live_validation']:
            if case.get('verification')=='exact_alert_and_edge_lineage' and all(v['passed'] for v in case['live_validation']):exact_verified+=1
            live_attempted+=1
            for variant in case['live_validation']:
                if variant['passed']:
                    exercised_em.update(s['episode_id'] for s in variant['steps'])
                    if ref['kind']=='PM':
                        verified_pm.add(ref['pm']);verified_routes.add(variant['variant'])
            if any(v['passed'] for v in case['live_validation']):live_pass+=1
            else:
                reasons={s['status'] for v in case['live_validation'] for s in v['steps'] if s['status']!='ok'}
                failures.update(reasons or {'witness_not_found'})
                db_incomplete.append({'case':case['id'],'reasons':sorted(reasons or {'witness_not_found'})})
    assert len(index)==offline_pass
    report={**summary,'training_total':len(manifest['items']),'offline_paths_verified':offline_pass,
      'live_cases_attempted':live_attempted,'live_cases_with_verified_variant':live_pass,
      'validated_em_count':len(exercised_em),'validated_pm_count':len(verified_pm),'validated_typed_routes':len(verified_routes),
      'exact_lineage_all_inputs_verified':exact_verified,'live_failure_reasons':dict(failures),'cases_with_literal_start_anchor':anchors,
      'case_step_distribution':dict(steps),'incidents':dict(incidents),
      'all_cases_processed':len(index)==len(manifest['items']),
      'validation_scope':'Original-log temporary MySQL projection; exported merged PM/EM; graph seed supplied; exact alert and edge witnesses checked AFTER complete execution. Not autonomous QA accuracy.',
      'live_unresolved_cases':db_incomplete}
    write_json(ROOT/'validation_report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='live_unresolved_cases'},ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':audit()
