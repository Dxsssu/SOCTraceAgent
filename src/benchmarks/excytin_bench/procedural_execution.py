"""Sequential dispatch of preallocated ITT tasks for a selected PM flow."""
from dataclasses import replace

from src.schema import TTTNodeStatus
from src.schema.ttt_updates import change_node


class ProceduralExecutionMixin:
    def _annotate_procedural_task(self, tree, node_id, context, *, state=None, error=None):
        pm = self._procedural_session
        run = pm.runs[context['run']]
        snippet = pm.offered[context['snippet_id']]
        dependencies = [run['tasks'][edge['from']] for edge in snippet['bindings']
                        if edge['to'] == context['step_id']]
        node = self._find_node(tree, node_id)
        detail = {'id': context['snippet_id'], 'run': context['run'], 'step': context['step_id'],
                  'episodic_id': context['episodic_id'],
                  'depends_on': list(dict.fromkeys(dependencies)),
                  'binding_state': state or ('waiting_upstream' if context.get('deferred') else 'bound')}
        if error:
            detail['error'] = error
        return change_node(tree, node_id, metadata={**node.metadata, 'procedure': detail})

    def _stop_procedural_run(self, tree, run_id, reason):
        pm = self._procedural_session
        run = pm.runs[run_id]
        run.update(status='interrupted', reason=reason)
        for step_id, task_id in run['tasks'].items():
            if step_id in run['observations']:
                continue
            tree = self._annotate_procedural_task(tree, task_id, pm.tasks[task_id],
                                                  state='blocked', error=reason)
            tree = change_node(tree, task_id, status=TTTNodeStatus.BLOCKED)
        self._record_trace('procedural_flow_interrupted', {'run': run_id, 'reason': reason})
        return tree

    def _advance_procedural_flow(self, execution):
        """True means another PM task is ready; no Planner/Verifier round is needed."""
        pm = self._procedural_session
        context = self._task_procedural_contexts.get(execution.node_id)
        if not pm or not context or not context.get('scheduled'):
            return False
        state = self._require_state()
        run_id = context['run']
        run = pm.runs[run_id]
        if not execution.query_success:
            state.ttt = self._stop_procedural_run(state.ttt, run_id, 'upstream_sql_failed')
            return False
        snippet = pm.offered[run['id']]
        pending = [step['id'] for step in snippet['steps'] if step['id'] not in run['observations']]
        if not pending:
            run['status'] = 'completed'
            self._record_trace('procedural_flow_completed', {'run': run_id, 'id': run['id']})
            return False
        if state.actions_issued >= self.max_steps - 1:
            state.ttt = self._stop_procedural_run(state.ttt, run_id, 'query_budget_exhausted')
            return False
        task_id = run['tasks'][pending[0]]
        try:
            bound = pm.bind_scheduled(task_id)
        except ValueError as exc:
            reason = str(exc)
            if reason in {'ambiguous_dependency_requires_explicit_selection', 'input_contract_mismatch'}:
                run.update(status='paused', reason=reason)
                state.ttt = self._annotate_procedural_task(state.ttt, task_id, pm.tasks[task_id],
                                                          state='needs_input', error=reason)
                self._record_trace('procedural_flow_needs_input',
                                   {'run': run_id, 'task_id': task_id, 'reason': reason})
            else:
                state.ttt = self._stop_procedural_run(state.ttt, run_id, reason)
            return False
        pm.register(task_id, bound)
        self._task_procedural_contexts[task_id] = bound
        state.ttt = self._annotate_procedural_task(state.ttt, task_id, bound)
        state.ttt = replace(state.ttt, next_task_id=task_id)
        state.ttt_history.append(state.ttt.to_dict())
        self._record_trace('procedural_step_ready', {'run': run_id, 'task_id': task_id,
                                                    'step': pending[0], 'bindings': bound['bindings']})
        return True
