"""Sequential full-corpus integration replay of the exported merged memories."""
import json
from datetime import datetime,timezone
from .core import ROOT,write_json
from .backend import RawLogSource,ReplayBackend
from .runtime import MemoryBundle
from .replay_one import replay


def main():
    manifest=json.loads((ROOT/'manifest.json').read_text())
    index=json.loads((ROOT/'case_memory_index.json').read_text())['cases']
    bundle=MemoryBundle();source=RawLogSource()
    backend=None;incident=None;results=[];source_manifests={}
    try:
        for item in manifest['items']:
            current=item['id'].split('_train_')[0]
            if current!=incident:
                if backend:backend.close();backend=None
                incident=current;backend=ReplayBackend(current,source)
                source_manifests[current]=backend.manifest
            result=replay(item['id'],backend,bundle,index);results.append(result)
            write_json(ROOT/'replay_progress.json',{'processed':len(results),'total':len(manifest['items']),
                'passed':sum(x['passed'] for x in results),'last_case':item['id']})
            if len(results)%10==0 or len(results)==len(manifest['items']):
                print(json.dumps({'processed':len(results),'passed':sum(x['passed'] for x in results),
                  'all_inputs_passed':sum(x['all_variants_passed'] for x in results),'last':item['id']}),flush=True)
    finally:
        if backend:backend.close()
        report={'utc':datetime.now(timezone.utc).isoformat(),'total_expected':len(manifest['items']),
          'processed':len(results),'passed':sum(x['passed'] for x in results),
          'all_variants_passed':sum(x['all_variants_passed'] for x in results),
          'sql_queries':sum(x['queries'] for x in results),'source_manifests':source_manifests,'cases':results,
          'scope':'repaired raw-log MySQL temporary projection; not original corrupted tables or autonomous QA accuracy'}
        write_json(ROOT/'full_replay_v2.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases','source_manifests')},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
