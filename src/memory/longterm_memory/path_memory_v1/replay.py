"""Opt-in read-only replay; run from the repository root."""
import hashlib
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from .store import MemoryStore, ROOT


def main():
    fixture = json.loads((ROOT / 'training_example.json').read_text())
    from pathlib import Path
    for field in ('source', 'graph'):
        assert hashlib.sha256(Path(fixture[f'{field}_file']).read_bytes()).hexdigest() == fixture[f'{field}_sha256']
    import networkx as nx
    graph = nx.read_graphml(fixture['graph_file'])
    path = [str(n) for n in fixture['question_record']['shortest_alert_path']]
    assert all(graph.has_edge(a, b) for a, b in zip(path, path[1:]))
    assert nx.shortest_path_length(graph, path[0], path[-1]) == len(path)-1
    load_dotenv('docker/excytin-mysql/.env')
    db = MySQLQueryExecutor('incident_55', password=os.environ['MYSQL_ROOT_PASSWORD'])
    try:
        def query(sql, params):
            db.cursor.execute(sql, params)
            fields = [col[0] for col in db.cursor.description]
            return [dict(zip(fields, row)) for row in db.cursor.fetchall()]
        result = MemoryStore().run('pm_url_account_device_url', fixture['inputs'], query)
    finally:
        db.close()
    # Gold is used only AFTER execution, never supplied to the memory runner.
    gold = fixture['question_record']['answer']
    result['evaluation'] = {
        'example_id': fixture['id'],
        'utc': datetime.now(timezone.utc).isoformat(),
        'official_path_verified': True,
        'gold': gold,
        'exact_match': result['candidates'] == [{'RemoteUrl': gold}],
        'match_ignoring_trailing_slash': len(result['candidates']) == 1 and result['candidates'][0]['RemoteUrl'].rstrip('/') == gold.rstrip('/'),
        'scope': fixture['scope_origin'],
    }
    (ROOT / 'replay_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': result['status'], 'rows_per_step': [len(t.get('rows', [])) for t in result['trace']], 'candidates': result['candidates'], 'evaluation': result['evaluation']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
