import os,json,time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv('.env');load_dotenv('docker/excytin-mysql/.env')
os.environ['SOCAGENT_LTM_BACKEND']='snapshot'
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
root=Path(__file__).parent
llm=TrackedLLM(None);calls=[]
def model(system,prompt):
 start=time.monotonic();response=llm(system,prompt)
 calls.append({'system':system,'input':prompt,'response':response,'seconds':time.monotonic()-start})
 (root/'first-step-calls.json').write_text(json.dumps({'calls':calls,'usage':dict(llm.usage)},ensure_ascii=False,indent=2)+'\n')
 return response
core=json.loads(Path('outputs/token_comparison/no-ref-source-check-20261012/manifest.json').read_text())
w=ExcytinBenchWorkflow(llm=model)
w.start({k:core[k] for k in ('context','question')})
a=w.propose_next_action()
db=MySQLQueryExecutor('incident_5',password=os.environ['MYSQL_ROOT_PASSWORD'])
try:
 observation,ok,count=db.execute(a.content)
finally:db.close()
result={'scope':'first query only, no final-answer scoring','sql':a.content,'success':ok,'rows':count,'usage':dict(llm.usage),'workflow':w.get_logging()}
(root/'first-step.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='workflow'},ensure_ascii=False))
