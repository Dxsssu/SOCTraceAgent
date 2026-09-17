"""Validated progressive TTT operations shared by both investigation runtimes."""
from dataclasses import replace
from typing import Any, Iterable, Mapping

from .event import utc_now
from .ttt import TracebackTaskTree, TTTNode, TTTNodeStatus as S


def walk(tree: TracebackTaskTree):
    def visit(node, depth):
        yield node, depth
        for child in node.children:
            yield from visit(child, depth + 1)
    for root in tree.root_nodes:
        yield from visit(root, 1)


def next_task(tree: TracebackTaskTree) -> TTTNode | None:
    eligible = []
    def visit(node, depth, active=True):
        active = active and node.status not in {S.RESOLVED, S.BLOCKED, S.NOT_APPLICABLE, S.DONE}
        if depth == 3 and active and node.status == S.TODO:
            eligible.append(node)
        for child in node.children:
            visit(child, depth + 1, active)
    for root in tree.root_nodes:
        visit(root, 1)
    return next((n for n in eligible if n.node_id == tree.next_task_id), None)


def resolved(tree: TracebackTaskTree) -> bool:
    return bool(tree.root_nodes) and all(n.status == S.RESOLVED for n in tree.root_nodes)


def change_node(tree: TracebackTaskTree, node_id: str, **changes) -> TracebackTaskTree:
    if not any(n.node_id == node_id for n, _ in walk(tree)):
        raise ValueError(f"Unknown node: {node_id}")
    def update(n):
        return replace(n, children=tuple(update(c) for c in n.children), **(changes if n.node_id == node_id else {}))
    return replace(tree, root_nodes=tuple(update(n) for n in tree.root_nodes), version=tree.version + 1, updated_at=utc_now())


def validate(tree: TracebackTaskTree, known_evidence: Iterable[str] = ()) -> None:
    if len(tree.root_nodes) != 1:
        raise ValueError("TTT requires exactly one L1 goal")
    ids = set()
    known = set(known_evidence)
    for n, depth in walk(tree):
        if not n.node_id or n.node_id in ids:
            raise ValueError("TTT node IDs must be unique and nonempty")
        ids.add(n.node_id)
        if depth > 3 or n.level != depth or not n.title.strip():
            raise ValueError("Invalid TTT level/title")
        allowed = {S.OPEN, S.RESOLVED, S.BLOCKED, S.NOT_APPLICABLE} if depth < 3 else {S.TODO, S.IN_PROGRESS, S.DONE, S.BLOCKED, S.NOT_APPLICABLE}
        if n.status not in allowed:
            raise ValueError(f"Invalid status for L{depth}: {n.status}")
        if not set(n.evidence_refs) <= known:
            raise ValueError("Unknown evidence reference")
        if n.status == S.RESOLVED and (not n.result_summary.strip() or not n.evidence_refs):
            raise ValueError("Resolved questions require a result summary and evidence references")
    if tree.next_task_id is not None and next_task(tree) is None:
        raise ValueError("next_task_id must select an actionable TODO L3")


def parse_initial(payload: Mapping[str, Any], event_id: str, round_id: int) -> TracebackTaskTree:
    # Legacy snapshots may omit levels and use TODO/DONE on ancestors.
    def normalize(value, depth, path):
        if not isinstance(value, Mapping):
            raise ValueError("children must contain nested node objects, not node IDs; root_nodes contains only the L1 object")
        value = dict(value)
        value['node_id'] = str(value.get('node_id') or path)
        value['level'] = int(value.get('level') or depth)
        if depth < 3 and value.get('status', 'todo') in {'todo', 'in_progress', 'done'}:
            value['status'] = 'open'
        value['children'] = [normalize(c, depth + 1, f'{path}-{i}') for i, c in enumerate(value.get('children') or [], 1)]
        value.pop('metadata', None)
        return value
    roots = [normalize(n, 1, str(i)) for i, n in enumerate(payload.get('root_nodes') or payload.get('nodes') or [], 1)]
    tree = TracebackTaskTree.from_dict(dict(payload, event_id=event_id, round_id=round_id, root_nodes=roots, version=1))
    # Only legacy initial plans can omit explicit selection.
    if 'next_task_id' not in payload:
        if any(n.get('level') for n in payload.get('root_nodes', [])):
            raise ValueError('Initial plan must explicitly select next_task_id')
        first = next((n for n, d in walk(tree) if d == 3 and n.status == S.TODO), None)
        tree = replace(tree, next_task_id=first.node_id if first else None)
    validate(tree)
    return tree


def apply_updates(tree: TracebackTaskTree, payload: Mapping[str, Any], *, known_evidence: Iterable[str] = (), round_id: int | None = None) -> TracebackTaskTree:
    if payload.get('base_version') != tree.version:
        raise ValueError('Stale or missing TTT base_version')
    if payload.get('event_id', tree.event_id) != tree.event_id:
        raise ValueError('TTT event mismatch')
    candidate = tree
    for op in payload.get('updates', []):
        nodes = {n.node_id: n for n, _ in walk(candidate)}
        if op.get('operation') == 'add_node':
            parent = nodes[op['parent_id']]
            if parent.level >= 3:
                raise ValueError('Cannot add children below L3')
            child = TTTNode.from_dict(op['node'], parent.level + 1)
            def append(n):
                return replace(n, children=n.children + (child,)) if n.node_id == parent.node_id else replace(n, children=tuple(append(c) for c in n.children))
            candidate = replace(candidate, root_nodes=tuple(append(n) for n in candidate.root_nodes))
        elif op.get('operation') == 'update_node':
            old = nodes[op['node_id']]
            changes = dict(op.get('changes') or {})
            if set(changes) - {'status', 'result_summary', 'evidence_refs'}:
                raise ValueError('Updates cannot change node identity, goal, or history')
            if 'status' in changes:
                changes['status'] = S(changes['status'])
                if old.status in {S.DONE, S.RESOLVED, S.BLOCKED, S.NOT_APPLICABLE} and changes['status'] in {S.TODO, S.OPEN} and not op.get('reason'):
                    raise ValueError('Reopening a node requires a reason')
            if 'evidence_refs' in changes:
                changes['evidence_refs'] = tuple(dict.fromkeys((*old.evidence_refs, *changes['evidence_refs'])))
            candidate = change_node(candidate, old.node_id, **changes)
        else:
            raise ValueError('Unsupported TTT update operation')
    if 'next_task_id' not in payload:
        raise ValueError('Planner must explicitly select next_task_id (or null)')
    candidate = replace(candidate, version=tree.version + 1, round_id=round_id or tree.round_id, next_task_id=payload['next_task_id'], selection_reason=str(payload.get('selection_reason') or ''), updated_at=utc_now())
    validate(candidate, known_evidence)
    if next_task(candidate) is None and candidate.root_nodes[0].status not in {S.RESOLVED, S.BLOCKED}:
        raise ValueError('Open goal needs an actionable task; expand a deferred question or mark blocked')
    return candidate
