import json
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv
import yaml
from src.agent.verifier import VERIFIER_SYSTEM_PROMPT,validate_verification
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.workflow.context import MemoryContextService,ExcytinBenchSnapshotRepository
from src.schema import Event,TracebackTaskTree,RoundReview

load_dotenv('.env')
root=Path('outputs/token_comparison/current-workflow')
manifest=json.loads((root/'manifest.json').read_text())
old=json.loads((root/'ours/incident_5-q0028/result.json').read_text())['workflow']
oc=json.loads((root/'opencode/incident_5-q0028/result.json').read_text())
llm=TrackedLLM(None)
report={'mode':'fixed-evidence replay, not an end-to-end success-rate estimate','checks':[]}
core={k:manifest[k] for k in ('context','question')}
for label,answer,records in [
 ('reject_script_host','powershell.exe',old['executions']),
 ('confirm_direct_alert_relation','browsercore.exe',[dict(execution_id=f'E{i+1}',sql=a['sql'],observation=a['observation'],query_success=a['success'],result_status='rows_returned' if a['success'] and a['row_count'] else 'error',observation_truncated='Displaying first' in a['observation']) for i,a in enumerate(oc['actions']) if a['type']=='execute_sql'])]:
 before=dict(llm.usage)
 prompt=dict(core,goals=[{'id':'G1','objective':core['question']}],candidate_answer=answer,executions=records)
 raw=llm(VERIFIER_SYSTEM_PROMPT,json.dumps(prompt,ensure_ascii=False))
 verdict=yaml.safe_load(raw)
 try:
  validate_verification(verdict,{'G1'},[SimpleNamespace(**e) for e in records])
 except ValueError as exc:
  verdict={'confirmed':False,'validation_error':str(exc),'raw_model_response':verdict}
 row={'case':label,'verdict':verdict,'usage':{k:v-before.get(k,0) for k,v in llm.usage.items()}}
 report['checks'].append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 Path('outputs/verifier_checks/replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
service=MemoryContextService(repositories={'excytin_bench':ExcytinBenchSnapshotRepository()},procedure_selector=llm)
event=Event(event_id='cost-replay',event_name='cost-replay',message=core['question'],context={'memory_profile':'excytin_bench','investigation_input':core})
states=[None]+[TracebackTaskTree.from_dict(e['payload']['ttt']) for e in old['trace'] if e['event_type']=='ttt_execution_updated']
report['pm_replay']=[]
for i,tree in enumerate(states):
 before=dict(llm.usage)
 review=None if i==0 else RoundReview(event_id=event.event_id,round_id=i,findings=(old['reviews'][i-1]['summary'],),recommendations=(old['reviews'][i-1]['suggestion'],))
 context=service.planner_view().build(event,ttt=tree,review=review,executions=[SimpleNamespace(**e,to_dict=lambda e=e:e) for e in old['executions'][:i]])
 row={'round':i+1,'procedures':[p['name'] for p in context['procedural_memory']['procedures']], 'reuse':context['procedural_memory'].get('reuse'), 'usage':{k:v-before.get(k,0) for k,v in llm.usage.items()}}
 report['pm_replay'].append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 Path('outputs/verifier_checks/replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
