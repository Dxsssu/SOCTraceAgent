"""Run vendored ExCyTIn evaluator logic with an injectable SDK transport.

Prompts, reflection ordering and reward calculation come directly from the
upstream file. Azure/autogen configuration imports are deliberately not loaded.
"""
import ast
import json
from pathlib import Path
import re
from textwrap import dedent
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT/'data/excytin-bench/github/secgym/evaluator.py'
UTILS = ROOT/'data/excytin-bench/github/secgym/utils/utils.py'


def load_evaluator():
    tree = ast.parse(SOURCE.read_text())
    definitions = [n for n in tree.body if isinstance(n, ast.Assign) or
                   isinstance(n, ast.ClassDef) and n.name in {'Evaluator', 'LLMEvaluator'}]
    helpers = [n for n in ast.parse(UTILS.read_text()).body if isinstance(n, ast.FunctionDef)
               and n.name in {'msging', 'get_full_question'}]
    scope = {'json': json, 're': re, 'dedent': dedent}
    exec(compile(ast.Module(body=helpers+definitions, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope['LLMEvaluator']


OfficialEvaluator = load_evaluator()


class SDKEvaluator(OfficialEvaluator):
    """Only replace API transport; inherit all official grading logic."""
    def __init__(self, request):
        super().__init__(config_list=[], ans_check_reflection=True,
                         sol_check_reflection=True, step_checking=True, strict_check=False)
        self.request = request

    def _retry_create(self, messages, match_pattern=None, **kwargs):
        for _ in range(10):
            response = self.request(messages, False)
            if response is not None and (match_pattern is None or re.search(match_pattern, response)):
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response))])
        raise ValueError('Official answer evaluator did not return Is_Answer_Correct after 10 attempts')

    def _get_json_response(self, system_prompt, task):
        for _ in range(10):
            response = self.request([{'role':'system','content':system_prompt},
                                     {'role':'user','content':task}], True)
            try:
                if '```json' in response: response = response.split('```json')[1].split('```')[0]
                parsed = json.loads(response)
                for value in parsed.values(): value['is_step_correct']
                return parsed, True
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
        # Do not quietly turn a judge failure into an incorrect investigation.
        raise ValueError('Official step evaluator did not return valid JSON after 10 attempts')
