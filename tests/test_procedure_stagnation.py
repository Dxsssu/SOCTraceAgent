from scripts.audit_procedure_stagnation import inspect_result


def test_probe_is_chronological_and_new_values_are_not_stagnation():
    retrieval = {'event_type': 'memory_retrieved',
                 'payload': {'card_retrieval': {'selected_ids': ['message_to_sender']}}}
    review = {'event_type': 'review_completed', 'payload': {'review': {
        'facts': [{'NetworkMessageId': 'new-message'}]}}}
    result = {'case_id': 'test', 'split': 'train', 'mode': 'pm', 'status': 'completed',
              'workflow': {'trace': [retrieval, retrieval, review, retrieval]}}
    report = inspect_result(result)
    assert [r['unique_fact_count'] for r in report['rounds']] == [0, 0, 1]
    assert report['consecutive_same_cards'] == 2
    assert report['consecutive_same_cards_and_facts'] == 1
    result['split'] = 'test'
    assert inspect_result(result) is None
