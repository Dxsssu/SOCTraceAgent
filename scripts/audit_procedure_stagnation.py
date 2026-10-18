"""Inspect train-run retrieval repetition without treating it as SQL repetition.

This is a diagnostic only: repeated cards may legitimately apply to new values.
Read chronological trace events so no future observation enters a prior state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def inspect_result(result):
    if result.get('split') != 'train' or result.get('mode') != 'pm':
        return None
    facts = set()
    previous = None
    rounds = []
    for trace in result.get('workflow', {}).get('trace', []):
        payload = trace.get('payload', {})
        if trace.get('event_type') == 'review_completed':
            for fact in payload.get('review', {}).get('facts', []):
                facts.add(json.dumps(fact, ensure_ascii=False, sort_keys=True))
        if trace.get('event_type') != 'memory_retrieved':
            continue
        retrieval = payload.get('card_retrieval') or {}
        if not retrieval:
            continue
        ids = tuple(retrieval.get('selected_ids', []))
        signature = hashlib.sha256('\n'.join(sorted(facts)).encode()).hexdigest()
        state = {'selected_ids': list(ids), 'unique_fact_count': len(facts),
                 'fact_fingerprint': signature,
                 'same_cards_as_previous': bool(ids and previous and previous[0] == ids),
                 'same_cards_and_facts': bool(ids and previous == (ids, signature))}
        rounds.append(state)
        previous = ids, signature
    frequencies = Counter(tuple(r['selected_ids']) for r in rounds if r['selected_ids'])
    usage = result.get('usage', {})
    return {'case_id': result['case_id'], 'status': result['status'],
            'tokens': usage.get('input_tokens', 0) + usage.get('output_tokens', 0),
            'queries': result.get('query_count'), 'retrievals': len(rounds),
            'consecutive_same_cards': sum(r['same_cards_as_previous'] for r in rounds),
            'consecutive_same_cards_and_facts': sum(r['same_cards_and_facts'] for r in rounds),
            'most_frequent_routes': [{'ids': list(ids), 'count': count}
                                     for ids, count in frequencies.most_common(3)],
            'rounds': rounds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for run in args.runs:
        for path in sorted((run / 'pm').glob('incident_*.json')):
            if path.stem.endswith('-audit'):
                continue
            result = json.loads(path.read_text())
            row = inspect_result(result)
            if row is not None:
                rows.append({'source': str(path), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **row})
    args.output.write_text(json.dumps({'scope': 'Train-only historical diagnostics across versions; not a pooled effectiveness estimate. Repeated cards do not prove repeated SQL or unnecessary work. Fact fingerprints exclude source IDs but do not normalize semantic equivalence.', 'cases': rows}, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'cases': len(rows), 'retrievals': sum(r['retrievals'] for r in rows),
                      'same_cards_and_facts': sum(r['consecutive_same_cards_and_facts'] for r in rows)}))


if __name__ == '__main__':
    main()
