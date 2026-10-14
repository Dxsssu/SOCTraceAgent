import json
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.schema import Event, TracebackTaskTree
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
load_dotenv('.env')
load_dotenv('docker/excytin-mysql/.env')
import os
from src.benchmarks.external_agents.environment import PaperQueryExecutor
db=PaperQueryExecutor('incident_5',password=os.environ['MYSQL_ROOT_PASSWORD'])
sql="SELECT Title, FileName, AlertId FROM AlertEvidence WHERE Title LIKE '%Primary Refresh Token%' AND EntityType = 'Process';"
observation,success,count=db.execute(sql)
db.close()
assert success and count
direct=[dict(execution_id='E_direct',node_id='T_verify',sql=sql,observation=observation,query_success=True,result_status='rows_returned',observation_truncated=False)]
r=Path('outputs/token_comparison/current-workflow')
a=json.loads((r/'ours/incident_5-q0028/result.json').read_text())['workflow']
b=json.loads((r/'opencode/incident_5-q0028/result.json').read_text())
m=json.loads((r/'manifest.json').read_text()); core={k:m[k] for k in ('context','question')}
llm=TrackedLLM(None); out=[]
for name,answer,records in [('script_host','powershell.exe',a['executions']),('direct_relation','browsercore.exe',direct)]:
 workflow=ExcytinBenchWorkflow(llm=llm)
 workflow.state=SimpleNamespace(event=Event(event_id='verify-replay',event_name='replay',message=core['question'],context={'investigation_input':core}),ttt=TracebackTaskTree.from_dict(a['ttt']),executions=[SimpleNamespace(**e,to_dict=lambda e=e:e) for e in records],verifications=[],trace=[])
 before=dict(llm.usage);v=workflow._verify_answer(answer)
 out.append({'case':name,'verdict':v,'usage':{k:x-before.get(k,0) for k,x in llm.usage.items()}})
 print(json.dumps(out[-1],ensure_ascii=False),flush=True)
 Path('outputs/verifier_checks/gate_replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
