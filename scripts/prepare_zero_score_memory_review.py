"""Freeze completed zero-reward cases and verify their supplied official graph paths.

Run from the repository root. Creates review packets; never changes runtime memory.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json,hashlib
from pathlib import Path
import networkx as nx
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.memory.longterm_memory.procedural_memory.flows import load_runtime_catalog
root=Path('runtime/pm_zero220_official_20261019');(root/'packets').mkdir(exist_ok=True)
base=Path('runtime/procedural_workflow_fancybag/deepseek_all_25');graphs={};records=[]
old={r['case_id']:r for r in json.loads(Path('src/memory/longterm_memory/procedural_memory/flows/failure_review/cases.json').read_text())}
for c in load_test_cases():
 score_path=base/'scoring/full'/f'{c.case_id}.json';score=json.loads(score_path.read_text())
 if score.get('source_status')!='completed' or score.get('paper_evaluation',{}).get('reward')!=0:continue
 graph_path=Path('data/excytin-bench/github/secgym/qagen/graph_files')/f'{c.incident}.graphml'
 if c.incident not in graphs:graphs[c.incident]=nx.read_graphml(graph_path)
 g=graphs[c.incident];q=c.question;path=list(map(str,q['shortest_alert_path']));nodes={}
 for i in set(path+list(map(str,q['start_entities']+q['end_entities']))):
  d=dict(g.nodes[i]);d['node_id']=i
  if 'entry' in d:d['entry']=json.loads(d['entry'])
  nodes[i]=d
 v={'exists':path[0]==str(q['start_alert']) and path[-1]==str(q['end_alert']) and all(g.has_edge(a,b) for a,b in zip(path,path[1:])),'given_edges':len(path)-1,'shortest_edges':nx.shortest_path_length(g,str(q['start_alert']),str(q['end_alert']))}
 record={'case_id':c.case_id,'incident':c.incident,'question':q,'official_nodes':nodes,'path_validation':v,'previous_review':old.get(c.case_id),'previous_submitted_answer':score.get('answer'),'source_sha256':{'score':hashlib.sha256(score_path.read_bytes()).hexdigest(),'graph':hashlib.sha256(graph_path.read_bytes()).hexdigest()},'official_graph_path':str(graph_path)}
 (root/'packets'/f'{c.case_id}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');records.append({'case_id':c.case_id,'incident':c.incident,'path_validation':v})
s=load_runtime_catalog();(root/'baseline_catalog.json').write_text(json.dumps(s.document,ensure_ascii=False))
manifest={'source_batch':str(base),'selection':'source_status=completed AND paper_evaluation.reward=0','cases':records,'count':len(records),'baseline_catalog_id':s.document['catalog_id'],'provenance':'evaluation_zero_score_official_path_adaptation'}
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');print('cases',len(records),'nonshortest',sum(x['path_validation']['given_edges']!=x['path_validation']['shortest_edges'] for x in records))
