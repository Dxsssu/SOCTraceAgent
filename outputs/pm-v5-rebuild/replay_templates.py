import json,os
from pathlib import Path
from dotenv import load_dotenv
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import *
from src.memory.longterm_memory.procedural_memory.excytin_bench.templates import render_sql
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
load_dotenv('.env');load_dotenv('docker/excytin-mysql/.env')
root=Path(__file__).parent
k=json.loads(DEFAULT_KNOWLEDGE_PATH.read_text());audit=json.loads(DEFAULT_KNOWLEDGE_PATH.with_name('build_audit.json').read_text())
s=retrieval_view(json.loads(DEFAULT_SEMANTIC_KNOWLEDGE.read_text()));schema={t['name']:t for t in s['tables']}
h=build_training_history(trajectories_path=DEFAULT_TRAJECTORIES,questions_dir=DEFAULT_QUESTIONS_DIR,semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE)
m=map_trajectories_to_train(load_jsonl(DEFAULT_TRAJECTORIES),load_train_questions(DEFAULT_QUESTIONS_DIR));rs=records_from_training(h['episodes'],m,s)
by_id={(r['episode_id'],r['attempt_id']):r for r in rs};connections={};results=[]
try:
 for p in k['procedures']:
  seen=set();samples=[]
  for ref in audit['templates'][p['id']]['support']:
   if ref['incident'] in seen:continue
   seen.add(ref['incident']);samples.append(ref)
  if len(samples)<2:
   samples=audit['templates'][p['id']]['support'][:2]
  for ref in samples[:2]:
   r=by_id[(ref['episode_id'],ref['attempt_id'])];binding=normalize_record(r,schema)[3]
   sql=render_sql(p,binding);incident=ref['incident']
   if incident not in connections:connections[incident]=MySQLQueryExecutor(incident,password=os.environ['MYSQL_ROOT_PASSWORD'])
   db=connections[incident]
   _,explain_ok,_=db.execute('EXPLAIN '+sql)
   observation,ok,count=db.execute(sql)
   results.append({'template_id':p['id'],'status':p['status'],'source':ref,'explain_success':explain_ok,'query_success':ok,'row_count':count,'error':observation if not ok else ''})
  (root/'sql_validation.json').write_text(json.dumps({'source_split':'train','queries':results,'templates':len(k['procedures']),'queries_passed':sum(x['query_success'] and x['explain_success'] and x['row_count']>0 for x in results)},ensure_ascii=False,indent=2)+'\n')
finally:
 for db in connections.values():db.close()
print(json.dumps({'queries':len(results),'passed':sum(x['query_success'] and x['explain_success'] and x['row_count']>0 for x in results),'failed':[x for x in results if not x['query_success'] or not x['explain_success'] or not x['row_count']]},ensure_ascii=False))
