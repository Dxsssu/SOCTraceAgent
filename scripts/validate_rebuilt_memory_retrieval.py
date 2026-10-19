"""Replay unedited Planner queries against the reviewed rebuilt memory catalog."""
import json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.procedural_memory.flows.catalog import apply_adaptations
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
R=ROOT/'runtime/pm_rebuild_regression8_20261019';P=ROOT/'runtime/procedural_workflow_fancybag/zero220_updated_regression10_20261019'
load_dotenv(ROOT/'docker/excytin-mysql/.env')
store=apply_adaptations(TrajectorySnippetStore.load(ROOT/'runtime/pm_zero220_official_20261019/baseline_catalog.json'),json.loads((R/'rebuild_overlay.json').read_text()))
expected={
'incident_39-q0014':'pm_eval_zero220_identity_f3d2e3acd22496dd2b27d60e',
'incident_134-q0043':'pm_eval_zero220_identity_521ca65594f130a3984f386f',
'incident_166-q0042':'pm_zero_cloud_e3bf59a587207ffa2e24909c',
'incident_5-q0019':'pm_reg8_4085532134fc2fd12ec40bf4',
'incident_34-q0069':'em_reg8_55e071326730033ef90903a7',
'incident_34-q0052':'pm_reg8_4208bed59ffe96401498256e',
'incident_55-q0050':'pm_reg8_78b65ce27c57ab461acba78f',
'incident_134-q0045':'proc_3fd1dcee652f186d8f222ad6'}
original={r['case_id']:r for r in json.loads((P/'summary.json').read_text())};probe=json.loads((R/'planner_probe.json').read_text());schemas={};out=[]
for r in probe['results']:
 cid=r['case_id'];inc=cid.split('-q')[0]
 if inc not in schemas:
  db=MySQLQueryExecutor(inc,password=os.environ['MYSQL_ROOT_PASSWORD']);schemas[inc]=db.semantic_schema();db.close()
 q=json.loads((ROOT/'runtime/pm_zero220_official_20261019/packets'/f'{cid}.json').read_text())['question'];tests=[]
 for mode,query in [('saved_original_query',original[cid]['procedural_retrievals'][0]['query']),('fresh_planner_query',r['query'])]:
  pm=ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schemas[inc]),default_intent=q['context']+' '+q['question'],goal_intent=q['question']),store)
  first=pm.retrieve(query);records=[first]
  if first['memory_type']=='procedural':records.append(pm.retrieve(query))
  ids=[c['id'] for rec in records for c in rec['candidates']]
  target=expected[cid];first_ids=[c['id']for c in first['candidates']]
  tests.append({'mode':mode,'query':query,'expected_memory':target,'first_route_hit':target in first_ids,'first_route_rank':first_ids.index(target)+1 if target in first_ids else None,
    'hit_after_explicit_pm_decline':target in ids,'records':records})
 out.append({'case_id':cid,'tests':tests})
 print(cid,[(t['mode'],t['first_route_hit'],t['hit_after_explicit_pm_decline'])for t in tests])
summary={mode:{'first_route_hits':sum(r['tests'][i]['first_route_hit']for r in out),
                   'first_route_top1':sum(r['tests'][i]['first_route_rank']==1 for r in out),
                   'hits_in_pm_then_em':sum(r['tests'][i]['hit_after_explicit_pm_decline']for r in out)}
         for i,mode in enumerate(['saved_original_query','fresh_planner_query'])}
(R/'retrieval_validation.json').write_text(json.dumps({'method':'Unedited real queries; PM first, repeat same query explicitly declines PM to inspect EM. No forced ID choice.','summary':summary,'results':out},ensure_ascii=False,indent=2)+'\n');print(summary)
