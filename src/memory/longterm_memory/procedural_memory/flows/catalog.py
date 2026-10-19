"""Deterministic split of the audited training catalog; no new SQL or evidence."""
from copy import deepcopy
import json
from pathlib import Path

from ..trajectory_snippets.core import digest
from ..trajectory_snippets.store import TrajectorySnippetStore

PACKAGE = Path(__file__).parent
SOURCE = PACKAGE.parent / 'trajectory_snippets/excytin/catalog.json'
EPISODIC = PACKAGE.parent.parent / 'episodic_memory/investigation_snippets/catalog.json'


def split_catalog(document):
    store = TrajectorySnippetStore(document)
    atoms = [deepcopy(s) for s in store.snippets.values() if len(s['steps']) == 1]
    atom_by_template = {}
    for atom in sorted(atoms, key=lambda s: s['id']):
        atom_by_template.setdefault(atom['steps'][0]['template_id'], atom['id'])
    templates = {tid: deepcopy(store.templates[tid]) for tid in sorted(atom_by_template)}
    em = {'format_version': 'trajectory-snippets-2', 'memory_type': 'episodic',
          'source_catalog_id': document['catalog_id'], 'snippets': atoms, 'templates': templates}
    em['catalog_id'] = digest(em)
    flows = [deepcopy(s) for s in store.snippets.values() if len(s['steps']) > 1]
    for flow in flows:
        for step in flow['steps']:
            # Missing atoms fail construction rather than manufacturing new experience.
            step['episodic_id'] = atom_by_template[step['template_id']]
    pm = {'format_version': 'procedural-flows-1', 'source_catalog_id': document['catalog_id'],
          'episodic_catalog_id': em['catalog_id'], 'flows': flows}
    pm['catalog_id'] = digest(pm)
    return pm, em


def compose_catalog(pm, em):
    if digest({k: v for k, v in pm.items() if k != 'catalog_id'}) != pm['catalog_id']:
        raise ValueError('procedural_catalog_digest_mismatch')
    atoms = TrajectorySnippetStore(em)
    if pm['format_version'] != 'procedural-flows-1' or em.get('memory_type') != 'episodic':
        raise ValueError('incorrect_memory_catalog_type')
    if pm['episodic_catalog_id'] != em['catalog_id']:
        raise ValueError('procedural_episodic_catalog_mismatch')
    if any(len(s['steps']) != 1 for s in atoms.snippets.values()):
        raise ValueError('episodic_requires_single_step')
    for flow in pm['flows']:
        if len(flow['steps']) < 2:
            raise ValueError('procedural_requires_multiple_steps')
        for step in flow['steps']:
            atom = atoms.snippets.get(step['episodic_id'])
            if not atom or atom['steps'][0]['template_id'] != step['template_id']:
                raise ValueError('flow_step_episodic_reference_mismatch')
    merged = {'format_version': 'trajectory-snippets-2',
              'snippets': deepcopy(pm['flows']) + deepcopy(em['snippets']),
              'templates': deepcopy(em['templates']),
              'memory_catalog_ids': {'procedural': pm['catalog_id'], 'episodic': em['catalog_id']}}
    merged['catalog_id'] = digest(merged)
    return TrajectorySnippetStore(merged)


def load_runtime_catalog():
    store = compose_catalog(json.loads((PACKAGE / 'catalog.json').read_text()),
                            json.loads(EPISODIC.read_text()))
    policy_path = PACKAGE / 'activation.json'
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        disabled = policy['disabled_flow_ids']
        if len(disabled) != len(set(disabled)) or any(
            sid not in store.snippets or len(store.snippets[sid]['steps']) < 2 for sid in disabled
        ):
            raise ValueError('invalid_disabled_procedural_ids')
        doc = deepcopy(store.document)
        doc['disabled_procedural_ids'] = disabled
        doc['memory_catalog_ids']['activation_policy'] = digest(policy)
        doc.pop('catalog_id')
        doc['catalog_id'] = digest(doc)
        store = TrajectorySnippetStore(doc)
    adaptations_path = PACKAGE / 'adaptations.json'
    if adaptations_path.exists():
        store = apply_adaptations(store, json.loads(adaptations_path.read_text()))
    official_path = PACKAGE / 'official_path_adaptations.json'
    if official_path.exists():
        overlay = json.loads(official_path.read_text())
        if overlay.get('origin') != 'evaluation_zero_score_official_path_adaptation':
            raise ValueError('official_path_adaptation_origin_required')
        previous_digest = store.document['memory_catalog_ids'].get('evaluation_adaptation')
        store = apply_adaptations(store, overlay)
        doc = deepcopy(store.document)
        doc['memory_catalog_ids']['official_path_adaptation'] = digest(overlay)
        if previous_digest:
            doc['memory_catalog_ids']['evaluation_adaptation'] = previous_digest
        doc.pop('catalog_id')
        doc['catalog_id'] = digest(doc)
        store = TrajectorySnippetStore(doc)
    coverage_path = PACKAGE / 'coverage_adaptations.json'
    if coverage_path.exists():
        store = apply_adaptations(store, json.loads(coverage_path.read_text()))
    contracts_path = PACKAGE / 'memory_contracts.json'
    if contracts_path.exists():
        contracts = json.loads(contracts_path.read_text())
        if contracts.get('format_version') != 'pm-contracts-1':
            raise ValueError('invalid_memory_contract_version')
        if any(tid not in store.templates or digest(store.templates[tid]) != signature
               for tid, signature in contracts['source_templates'].items()):
            raise ValueError('memory_contract_templates_changed_reaudit_required')
        doc = deepcopy(store.document)
        by_id = {s['id']: s for s in doc['snippets']}
        enabled = {sid for sid,s in by_id.items() if len(s['steps']) > 1 and sid not in doc.get('disabled_procedural_ids', [])}
        contract_ids = [c['id'] for c in contracts['contracts']]
        if len(contract_ids) != len(set(contract_ids)) or enabled != set(contract_ids):
            raise ValueError('memory_contract_enabled_pm_coverage_mismatch')
        for contract in contracts['contracts']:
            snippet = by_id.get(contract['id'])
            if not snippet or len(snippet['steps']) < 2:
                raise ValueError('memory_contract_unknown_pm')
            step_templates = {s['id']: store.templates[s['template_id']] for s in snippet['steps']}
            accounted = set()
            for slot in contract.get('known', []) + contract.get('scope_inputs', []):
                template = step_templates.get(slot['step'], {})
                spec = template.get('inputs', {}).get(slot['parameter'])
                if not spec or slot['field'] not in {spec['field'], spec.get('source_field')} or slot['field'] not in template.get('schema_fields', {}).get(slot['table'], []):
                    raise ValueError('memory_contract_input_not_in_sql')
                accounted.add((slot['step'],slot['parameter']))
            for edge in snippet['bindings']:
                for parameter,spec in step_templates[edge['to']]['inputs'].items():
                    if spec['field'] == edge['to_field'] and edge.get('to_parameter') in (None,parameter):
                        accounted.add((edge['to'],parameter))
            actual = {(step,p) for step,template in step_templates.items() for p in template['inputs']}
            if actual != accounted:
                raise ValueError('memory_contract_unaccounted_external_input')
            terminal = {st['id']: store.templates[st['template_id']]['outputs'] for st in snippet['steps']
                        if st['id'] not in {edge['from'] for edge in snippet['bindings']}}
            for field in ('wanted', 'effective_wanted', 'strict_wanted', 'candidate_goal_wanted'):
                if any(x.get('column') not in terminal.get(x.get('step', snippet['steps'][-1]['id']), []) for x in contract.get(field, [])):
                    raise ValueError('memory_contract_unprojected_terminal_output')
            snippet['memory_contract'] = deepcopy(contract)
        doc['memory_catalog_ids']['typed_memory_contracts'] = digest(contracts)
        doc.pop('catalog_id')
        doc['catalog_id'] = digest(doc)
        store = TrajectorySnippetStore(doc)
    for snippet in store.snippets.values():
        if len(snippet['steps']) != 1:
            continue
        template = store.templates[snippet['steps'][0]['template_id']]
        if any(not any(spec.get('source_field', spec['field']) in fields for fields in template['schema_fields'].values())
               for spec in template['inputs'].values()):
            raise ValueError('episodic_input_source_field_missing')
        for slot in snippet.get('required_inputs', []):
            spec = template['inputs'].get(slot['parameter'], {})
            if slot['field'] not in {spec.get('field'),spec.get('source_field')} or slot['field'] not in template['schema_fields'].get(slot['table'], []):
                raise ValueError('episodic_input_contract_mismatch')
    from ..trajectory_snippets.query_optimization import POLICY_VERSION
    doc = deepcopy(store.document)
    doc["query_optimization"] = POLICY_VERSION
    doc["memory_catalog_ids"]["query_optimization"] = POLICY_VERSION
    doc.pop("catalog_id")
    doc["catalog_id"] = digest(doc)
    return TrajectorySnippetStore(doc)


def apply_adaptations(store, adaptations):
    """Keep test-derived revisions separate from the immutable training partition."""
    if adaptations.get('provenance') != 'evaluation_failure_adaptation':
        raise ValueError('adaptation_provenance_required')
    doc = deepcopy(store.document)
    snippets = {s['id']: s for s in doc['snippets']}
    for tid, template in adaptations.get('new_templates', {}).items():
        if tid in doc['templates'] and doc['templates'][tid] != template:
            raise ValueError('adaptation_template_collision')
        doc['templates'][tid] = deepcopy(template)
    for item in adaptations.get('new_episodes', []) + adaptations.get('new_flows', []):
        if item['id'] in snippets:
            raise ValueError('adaptation_snippet_collision')
        snippets[item['id']] = deepcopy(item)
        doc['snippets'].append(snippets[item['id']])
    for update in adaptations.get('existing_memory_updates', []):
        sid = update['id']
        before = snippets[sid]
        after = deepcopy(update['snippet'])
        signature = lambda s: ([(st['id'], st['template_id']) for st in s['steps']], s.get('bindings', []))
        if (not update.get('source_cases') or not update.get('reason') or after['id'] != sid
                or signature(before) != signature(after)):
            raise ValueError('invalid_existing_memory_update')
        for contract in after.get('evidence_outputs', []):
            if contract['column'] not in doc['templates'][after['steps'][-1]['template_id']]['outputs']:
                raise ValueError('updated_evidence_output_not_projected')
        doc['snippets'][next(i for i, s in enumerate(doc['snippets']) if s['id'] == sid)] = after
        snippets[sid] = after
    for patch in adaptations.get('guidance_patches', []):
        snippet = snippets[patch['flow_id']]
        if len(snippet['steps']) < 2 or not patch['source_cases']:
            raise ValueError('invalid_flow_guidance_patch')
        guides = patch.get('step_guides', {})
        if set(guides) - {s['id'] for s in snippet['steps']}:
            raise ValueError('unknown_guidance_step')
        for step in snippet['steps']:
            if step['id'] in guides:
                step['planner_guide'] += '\n' + guides[step['id']]
        snippet['caution'] = list(dict.fromkeys(snippet['caution'] + patch.get('caution_add', [])))
        for variant in snippet.get('intent_variants', []):
            variant['caution'] = list(dict.fromkeys(variant['caution'] + patch.get('caution_add', [])))
            for guide in variant.get('step_guides', []):
                if guide['step'] in guides:
                    guide['guide'] += '\n' + guides[guide['step']]
        snippet.setdefault('adaptation_sources', []).append(patch['id'])
    for patch in adaptations.get('episode_guidance_patches', []):
        snippet = snippets[patch['episode_id']]
        if len(snippet['steps']) != 1 or not patch.get('source_cases'):
            raise ValueError('invalid_episode_guidance_patch')
        snippet['steps'][0]['planner_guide'] += '\n' + patch.get('guide', '')
        snippet['caution'] = list(dict.fromkeys(snippet['caution'] + patch.get('caution_add', [])))
        for variant in snippet.get('intent_variants', []):
            variant['caution'] = list(dict.fromkeys(variant['caution'] + patch.get('caution_add', [])))
            for guide in variant.get('step_guides', []):
                guide['guide'] += '\n' + patch.get('guide', '')
        snippet.setdefault('adaptation_sources', []).append(patch['id'])
    for flow in doc['snippets']:
        if len(flow['steps']) < 2:
            continue
        for step in flow['steps']:
            atom = snippets.get(step.get('episodic_id'))
            if not atom or len(atom['steps']) != 1 or atom['steps'][0]['template_id'] != step['template_id']:
                raise ValueError('adapted_flow_episodic_reference_mismatch')
    if any(len(s['steps']) != 1 for s in adaptations.get('new_episodes', [])) or any(
        len(s['steps']) < 2 for s in adaptations.get('new_flows', [])
    ):
        raise ValueError('adaptation_step_count_mismatch')
    doc['memory_catalog_ids']['evaluation_adaptation'] = digest(adaptations)
    doc['memory_provenance'] = 'training_catalog_plus_evaluation_failure_adaptation'
    doc.pop('catalog_id')
    doc['catalog_id'] = digest(doc)
    return TrajectorySnippetStore(doc)


def main():
    source = json.loads(SOURCE.read_text())
    pm, em = split_catalog(source)
    compose_catalog(pm, em)
    for path, data in [(PACKAGE / 'catalog.json', pm), (EPISODIC, em)]:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    audit = {'source_catalog_id': source['catalog_id'], 'procedural_catalog_id': pm['catalog_id'],
             'episodic_catalog_id': em['catalog_id'], 'procedural_flows': len(pm['flows']),
             'episodic_snippets': len(em['snippets']), 'sql_templates': len(em['templates']),
             'flow_steps': sum(len(f['steps']) for f in pm['flows']),
             'unmapped_flow_steps': 0, 'test_data_used': False,
             'method': 'Lossless partition by step count; flow steps reference existing atomic template owners'}
    (PACKAGE / 'split_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(audit))


if __name__ == '__main__':
    main()
