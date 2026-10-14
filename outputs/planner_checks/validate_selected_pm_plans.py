"""Inspect initial plans using the frozen results of the final retrieval run."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import yaml
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.schema import Event
from src.schema.itt import itt_payload
from src.workflow.context import MemoryContextService

ROOT = Path('outputs/planner_checks/question-led-pm-final')
CASES = ['incident_166-q0053', 'incident_322-q0016', 'incident_5-q0028', 'incident_134-q0056']
def write(p, v):
 p.write_text(json.dumps(v, ensure_ascii=False, indent=2)+'\n')
def run(cid):
 source_path = (Path('outputs/planner_checks/question-led-pm-protocol-repair/cases') if cid == 'incident_322-q0016' else ROOT/'cases') / (cid+'.json')
 source=json.loads(source_path.read_text())
 if 'diagnostics' not in source:raise RuntimeError('Retrieval still running')
 core=source['input'];procedures=source.get('procedures',[])
 memory={'procedural_memory':{'procedures':procedures,'application':source['diagnostics'].get('application')}}
 folder=ROOT/'planner_validation'/cid;folder.mkdir(parents=True,exist_ok=False)
 calls=[];llm=TrackedLLM(LLMConfig.from_env().model)
 def logged(system,prompt):
  item={'system':system,'prompt':prompt};calls.append(item);write(folder/'calls.json',calls)
  item['response']=llm(system,prompt);write(folder/'calls.json',calls);return item['response']
 w=ExcytinBenchWorkflow(llm=logged,memory_contexts=MemoryContextService(repositories={}))
 event=Event(event_name='initial planner validation',message=core['question'],context={'investigation_input':core})
 record={'case_id':cid,'retrieval_source':str(source_path),'memory':memory,'input':core}
 try:
  tree=w._plan_initial_ttt(event,memory,investigation_input=core)
  record.update(success=True,itt=itt_payload(tree))
  (folder/'itt.yaml').write_text(yaml.safe_dump(record['itt'],allow_unicode=True,sort_keys=False))
 except Exception as exc:record.update(success=False,error=f'{type(exc).__name__}: {exc}')
 record.update(usage=dict(llm.usage));write(folder/'result.json',record)
 return record
if __name__=='__main__':
 with ThreadPoolExecutor(max_workers=3) as pool:
  for f in as_completed([pool.submit(run,c) for c in CASES]):
   r=f.result();print(r['case_id'],r['success'],flush=True)
