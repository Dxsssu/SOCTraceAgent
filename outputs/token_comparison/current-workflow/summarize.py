import json
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parent
summary={'case_id':'incident_5-q0028','model':'DeepSeek-V4.1-Flash','method':'Sum provider-reported usage for every model request, including repeated context and any post-terminal usage; no judge calls. Cached input is a subset, not added twice.','agents':{}}
for name in ['opencode','ours']:
    path=ROOT/name/summary['case_id']/'result.json'
    row=json.loads(path.read_text())
    if row['status']=='running': continue
    records=row.get('usage',[])+row.get('post_terminal_usage',[])
    counts=defaultdict(lambda:dict(calls=0,input_tokens=0,output_tokens=0,total_tokens=0))
    calls=[]
    for i,u in enumerate(records):
        kind=u.get('_request',{}).get('kind','unknown')
        inp=u.get('prompt_tokens',0);out=u.get('completion_tokens',0)
        counts[kind]['calls']+=1;counts[kind]['input_tokens']+=inp;counts[kind]['output_tokens']+=out;counts[kind]['total_tokens']+=inp+out
        calls.append(dict(call=i+1,role=kind,input_tokens=inp,output_tokens=out,total_tokens=inp+out,
                          messages=u.get('_request',{}).get('messages')))
    summary['agents'][name]=dict(status=row['status'],error=row.get('error',''),model_calls=row.get('llm_calls'),
        usage_records=len(records),missing_usage=sum('prompt_tokens' not in u or 'completion_tokens' not in u for u in records),
        input_tokens=sum(u.get('prompt_tokens',0) for u in records),output_tokens=sum(u.get('completion_tokens',0) for u in records),
        total_tokens=sum(u.get('prompt_tokens',0)+u.get('completion_tokens',0) for u in records),
        provider_total_tokens=sum(u.get('total_tokens',0) for u in records),
        cached_tokens=sum((u.get('prompt_tokens_details') or {}).get('cached_tokens',0) for u in records),
        post_terminal_calls=len(row.get('post_terminal_usage',[])),
        sql_queries=sum(a['type']=='execute_sql' for a in row['actions']),
        sql_errors=sum(a['type']=='execute_sql' and not a.get('success') for a in row['actions']),
        elapsed_seconds=row.get('elapsed_seconds'),answer=row.get('answer'),evaluation=row.get('evaluation'),
        by_role=dict(counts),calls=calls)
if len(summary['agents'])==2:
    ours=summary['agents']['ours']['total_tokens'];oc=summary['agents']['opencode']['total_tokens']
    summary['ours_vs_opencode']=dict(token_difference=ours-oc,ratio=ours/oc,savings_fraction=1-ours/oc)
(ROOT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
