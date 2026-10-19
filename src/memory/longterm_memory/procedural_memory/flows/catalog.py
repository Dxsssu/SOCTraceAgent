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
    return store


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
