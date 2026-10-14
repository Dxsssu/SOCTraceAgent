import sys,os,json,time,hashlib
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor,as_completed
from src.benchmarks.external_agents import batch
from src.benchmarks.external_agents.runner import run_case,write_json
from src.benchmarks.external_agents.evaluation import judge_config,score_case,protocol_manifest
from src.benchmarks.excytin_bench.runner import load_test_cases,deterministic_evaluation
from src.agent.llm import LLMConfig
load_dotenv('.env');load_dotenv('docker/excytin-mysql/.env')
os.environ['SOCAGENT_LLM_PROVIDER']='fancybag'
os.environ['FANCYBAG_MODEL']='deepseek-v4.1-flash'
os.environ['SOCAGENT_LTM_BACKEND']='snapshot'
root=Path(__file__).resolve().parent
case=next(x for x in load_test_cases() if x.case_id=='incident_5-q0028')
config=LLMConfig.from_env();judge=judge_config()
write_json(root/'manifest.json',dict(case=case.manifest_view(),model=config.model,provider='fancybag',base_url=config.base_url,memory_backend='snapshot',protocol=protocol_manifest([case],max_steps=25,judge=judge,timeout=900,max_llm_calls=0),source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('src').rglob('*.py')},pm_sha256=hashlib.sha256(Path('src/memory/longterm_memory/procedural_memory/excytin_bench/procedural_memory.json').read_bytes()).hexdigest()))
original=batch.model_call
calls=[]
def logged(bridge,messages,**kwargs):
 rec={'index':len(calls)+1,'messages':messages};calls.append(rec);start=time.monotonic();n=len(bridge.usage)
 try:
  rec['response']=original(bridge,messages,**kwargs);return rec['response']
 finally:
  rec['seconds']=time.monotonic()-start;rec['usage']=bridge.usage[n:];write_json(root/'ours-calls.json',calls)
  print('ours call',rec['index'],str(messages[0].get('content',''))[:65].replace('\n',' '),round(rec['seconds'],1),flush=True)
batch.model_call=logged
results={}
def run(agent):
 if agent=='ours':return batch.run_internal(agent,case,root,None,max_steps=25,timeout=900)
 return run_case(agent,case,root,max_steps=25,timeout=900,llm_eval=False)
with ThreadPoolExecutor(max_workers=2) as pool:
 futures={pool.submit(run,a):a for a in ['ours','opencode']}
 for f in as_completed(futures):
  a=futures[f]
  try:r=f.result()
  except Exception as exc:r={'agent':a,'status':'error','error':str(exc)}
  r['evaluation']=deterministic_evaluation(case.question['answer'],r.get('answer',''))
  results[a]=r;write_json(root/'comparison.json',results)
  print(a,r['status'],r.get('answer',''),flush=True)
for a,r in results.items():
 if r.get('status')!='error':
  try:score_case(case,r,judge)
  except Exception as exc:r['judge_error']=str(exc)
  write_json(root/a/case.case_id/'result.json',r)
write_json(root/'comparison.json',results)
for a,r in results.items():
 print(a,json.dumps({k:r.get(k) for k in ['status','answer','error','llm_calls','elapsed_seconds','evaluation','llm_evaluation']},ensure_ascii=False),flush=True)
