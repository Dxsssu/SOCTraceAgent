"""Replay three fixed initial tasks with/without previously admitted PM."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import json
import time
import yaml
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.schema import Event
from src.schema.itt import itt_payload
from src.workflow.context import MemoryContextService

SOURCE=Path('outputs/planner_checks/20261012-192601-pm-batch')
ROOT=Path('outputs/planner_checks')/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-planning-help')
ROOT.mkdir(parents=True)
CASES=['incident_322-q0014','incident_166-q0053','incident_5-q0028']
def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
def main():
 model=LLMConfig.from_env().model
 write(ROOT/'manifest.json',{'source':str(SOURCE),'cases':CASES,'model':model,'modes':['with_pm','without_pm'],'scope':'initial ITT only; frozen prior retrieval; one sample per condition, no SQL; qualitative comparison, not a causal efficacy estimate'})
 def run(cid,mode):
  source=json.loads((SOURCE/(cid+'.json')).read_text())
  core=source['input'];pm=source['selected_pm'] if mode=='with_pm' else []
  llm=TrackedLLM(model);calls=[];folder=ROOT/(cid+'-'+mode);folder.mkdir()
  result={'case_id':cid,'mode':mode,'input':core,'pm':pm}
  def logged(system,prompt):
   record={'system':system,'prompt':prompt};calls.append(record);write(folder/'calls.json',calls)
   begin=time.monotonic()
   try:
    record['response']=llm(system,prompt);return record['response']
   finally:
    record['seconds']=round(time.monotonic()-begin,3);write(folder/'calls.json',calls)
  workflow=ExcytinBenchWorkflow(llm=logged,memory_contexts=MemoryContextService(repositories={}))
  event=Event(event_name='planning comparison',message='\n'.join(core.values()),context={'investigation_input':core})
  try:
   tree=workflow._plan_initial_ttt(event,{'procedural_memory':{'procedures':pm}},investigation_input=core)
   result.update(success=True,itt=itt_payload(tree),next_task_id=tree.next_task_id)
   (folder/'itt.yaml').write_text(yaml.safe_dump(result['itt'],allow_unicode=True,sort_keys=False))
  except Exception as exc:result.update(success=False,error=f'{type(exc).__name__}: {exc}')
  result.update(usage=dict(llm.usage),attempts=len(calls));write(folder/'result.json',result)
  return result
 print('Artifacts:',ROOT,flush=True)
 results=[]
 with ThreadPoolExecutor(max_workers=3) as pool:
  for f in as_completed([pool.submit(run,c,m) for c in CASES for m in ['with_pm','without_pm']]):
   r=f.result();results.append(r);write(ROOT/'results.json',results)
   print(r['case_id'],r['mode'],'success=',r['success'],'attempts=',r['attempts'],flush=True)
if __name__=='__main__':main()
