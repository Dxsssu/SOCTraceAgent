import json,collections
from pathlib import Path
p=Path(__file__).parent
calls=json.loads((p/'ours-calls.json').read_text());roles=collections.defaultdict(lambda:dict(calls=0,input=0,output=0,seconds=0))
for c in calls:
 system=c['messages'][0]['content']
 role=next((r for r in ['Planner','Reviewer','Verifier'] if r in system[:150]),None)
 if not role:role='PM' if 'Procedural Memory' in system[:150] else 'SM' if 'SM 字段' in system[:150] else 'Executor' if 'SQL 查询生成器' in system[:150] else 'Answer'
 r=roles[role];r['calls']+=1;r['seconds']+=c.get('seconds',0)
 for u in c.get('usage',[]):r['input']+=u.get('prompt_tokens',0);r['output']+=u.get('completion_tokens',0)
print('roles',json.dumps(dict(roles),ensure_ascii=False))
results={}
for agent in ['ours','opencode']:
 f=p/agent/'incident_5-q0028/result.json'
 if not f.exists():continue
 r=json.loads(f.read_text());u=r.get('usage',[])
 out={k:r.get(k) for k in ['status','answer','error','elapsed_seconds','llm_calls','evaluation','llm_evaluation']}
 out.update(input=sum(x.get('prompt_tokens',0) for x in u),output=sum(x.get('completion_tokens',0) for x in u),sql=sum(x.get('type')=='execute_sql' for x in r.get('actions',[])),sql_errors=sum(x.get('type')=='execute_sql' and not x.get('success') for x in r.get('actions',[])))
 results[agent]=out
 print(agent,json.dumps(out,ensure_ascii=False))
(p/'metrics.json').write_text(json.dumps({'roles':dict(roles),'results':results},ensure_ascii=False,indent=2))
