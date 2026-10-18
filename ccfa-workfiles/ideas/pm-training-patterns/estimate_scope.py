"""Estimate remaining PM families from train SQL, not from test answers."""
import collections
import hashlib
import json
from pathlib import Path
import re
import sqlglot
from sqlglot import exp
import yaml

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'data/excytin-bench/github/secgym/agents/expel_train/corrects.jsonl'
DEFS=ROOT/'src/memory/longterm_memory/procedural_memory/excytin_bench/cards/definitions.yaml'
def main():
    definitions=yaml.safe_load(DEFS.read_text())
    groups=collections.defaultdict(list)
    for line, text in enumerate(SOURCE.read_text().splitlines(),1):
        row=json.loads(text)
        if row['value'].get('reward') != 1: continue
        messages=row['value']['messages']
        for mi,m in enumerate(messages[:-1]):
            match=re.search(r'Action:\s*execute\[(.*)\]\s*$',m.get('content',''),re.S|re.I) if m['role']=='assistant' else None
            if not match:continue
            obs=messages[mi+1]['content']
            if re.search(r'(?:ProgrammingError|OperationalError|DatabaseError|InterfaceError):',obs) or re.match(r'^\s*\[\s*\]',obs):continue
            if not (obs.lstrip().startswith('[') or re.match(r'^Retrieved [1-9]\d* entries\.',obs)):continue
            tree=sqlglot.parse_one(match.group(1),read='mysql');tables=list(tree.find_all(exp.Table))
            if not isinstance(tree,exp.Select) or len(tables)!=1 or len(list(tree.find_all(exp.Select)))!=1 or not tree.args.get('where'):continue
            table=tables[0].name; fields=sorted({c.name for c in tree.args['where'].find_all(exp.Column)})
            matches=[d['id'] for d in definitions if d['source_rule']['table']==table and set(d['source_rule']['filters'])<=set(fields)]
            groups[(table,tuple(fields))].append({'line':line,'message_index':mi,'sql':match.group(1),'matching_card_filters':matches})
    families=[]
    for (table,fields),queries in groups.items():
        matched=sorted({c for q in queries for c in q['matching_card_filters']})
        families.append({'table':table,'filter_fields':fields,'trajectory_count':len({q['line'] for q in queries}),'query_count':len(queries),'matching_card_filters':matched,'sources':queries})
    families.sort(key=lambda f:(bool(f['matching_card_filters']),-f['trajectory_count'],f['table']))
    report={'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'definition_sha256':hashlib.sha256(DEFS.read_bytes()).hexdigest(),'existing_cards':len(definitions),'filter_families':len(families),'unmatched_filter_families':sum(not f['matching_card_filters'] for f in families),'unmatched_repeated_families':sum(not f['matching_card_filters'] and f['trajectory_count']>=2 for f in families),'limitations':['Filter matching is an upper bound on possible coverage, not semantic or output coverage.','Different filter combinations can merge into one PM; one broad family can require multiple roles.','No-filter queries, joins, aggregates, schema discovery and multi-step control patterns need separate review.'],'families':families}
    Path(__file__).with_name('scope_inventory.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print({k:v for k,v in report.items() if k not in ('families','limitations')})
    for f in families:
        if not f['matching_card_filters']:print(f['table'],f['filter_fields'],f['trajectory_count'])
if __name__=='__main__':main()
