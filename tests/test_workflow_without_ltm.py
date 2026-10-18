"""Workflow startup must stay independent of the LTM being reconstructed."""
import os
from pathlib import Path
import subprocess
import sys


def test_workflows_start_with_ltm_imports_blocked(tmp_path):
    code = r'''
import importlib.abc
import sys
from unittest.mock import patch

class BlockLTM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith('src.memory.longterm_memory')
                or fullname == 'src.workflow.context' or fullname.startswith('neo4j')):
            raise AssertionError('Workflow imported detached LTM: ' + fullname)
sys.meta_path.insert(0, BlockLTM())
from src.workflow.orchestrator import SOCTraceWorkflow
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.agent.request_limits import memory_output_limit
from src.schema import Event

local = SOCTraceWorkflow(db_path=sys.argv[1])
event = Event(event_name='test', message='Find alert', context={'memory_profile': 'excytin_bench'})
plan = ''' + repr('''operations:
- {op: add_goal, ref: goal, objective: Find alert}
- {op: add_entity, ref: entity, goal_id: $goal, objective: Alert}
- {op: add_task, entity_id: $entity, objective: Query alert}
''') + r'''
with patch('src.agent.planner.call_llm', return_value=plan) as model:
    local.planner._generate_initial_ttt(event)
    assert 'Procedural Memory' not in model.call_args.args[0]
calls = []
def llm(system, user):
    calls.append((system, user))
    assert memory_output_limit(system) is None
    return plan
workflow = ExcytinBenchWorkflow(llm=llm)
workflow.start({'context': 'Alert', 'question': 'Find alert'})
assert len(calls) == 1
assert workflow.get_logging()['ltm_enabled'] is False
assert workflow.get_logging()['planner_pm_enabled'] is False
assert workflow.get_logging()['memory_retrievals'] == []
'''
    env = dict(os.environ, SOCAGENT_LTM_ENABLED='true', SOCAGENT_PLANNER_PM_ENABLED='true',
               SOCAGENT_MEMORY_PROFILE='excytin_bench', SOCAGENT_LTM_BACKEND='neo4j')
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'state.db')],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
