"""Upstream ExCyTIn evaluator, shared by CLI and paper-agent experiments."""
from __future__ import annotations
import ast
from dataclasses import replace
import hashlib
import json
import os
import re
from openai import OpenAI
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import PROJECT_ROOT
from .bridge import Bridge, thinking_parameters

UPSTREAM = PROJECT_ROOT / 'data/excytin-bench/github/secgym'
PROTOCOL_VERSION = 'excytin-paper-v1'

def paper_evaluator(bridge):
    """Keep upstream judge prompts/reflection/reward; replace provider imports only."""
    source = UPSTREAM / 'evaluator.py'
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and
        n.module in ('autogen', 'secgym.myconfig', 'secgym.utils.utils'))]

    class Transport:
        def __init__(self, **kwargs):
            pass

        def create(self, messages, **kwargs):
            with OpenAI(api_key=bridge.token, base_url=bridge.url + '/v1',
                        timeout=180, max_retries=0) as client:
                return client.chat.completions.create(
                    model=bridge.config.model, messages=messages, **kwargs)

    namespace = dict(OpenAIWrapper=Transport, config_list_4o=[],
                     msging=lambda msg, role='user': {'role': role, 'content': msg},
                     get_full_question=lambda q: f"{q.get('context', '')} {q['question']}".strip())
    exec(compile(tree, str(source), 'exec'), namespace)
    return namespace['LLMEvaluator'](
        config_list=[], cache_seed=41, ans_check_reflection=True,
        sol_check_reflection=True, step_checking=True, strict_check=False)


def validate_judgement(evaluation, question=None):
    verdict = evaluation.get('check_ans_reflection', '')
    match = re.search(r'Is_Answer_Correct: (True|False)', verdict)
    if not match:
        raise ValueError('Judge did not return a valid reflected answer verdict')
    if match.group(1) == 'True':
        if evaluation.get('reward') != 1:
            raise ValueError('Correct answer must have reward=1')
        return
    if not evaluation.get('is_json_success') or not evaluation.get('is_reflect_success'):
        raise ValueError('Judge step evaluation/reflection failed; reward is unavailable')
    steps = list(evaluation['check_sol_reflection'].values())
    if not steps or (question is not None and isinstance(question.get('solution'), list)
                     and len(steps) != len(question['solution'])):
        raise ValueError('Judge returned missing or incomplete solution steps')
    if any(s.get('is_step_correct') not in ('True', 'False') for s in steps):
        raise ValueError('Invalid step verdict')
    expected = sum(0.4 ** (len(steps) - 1 - i) for i, s in enumerate(steps[:-1])
                   if s['is_step_correct'] == 'True')
    if abs(evaluation['reward'] - min(1, expected)) > 1e-9:
        raise ValueError('Reward does not match the paper discount formula')


def judge_config(model=None):
    """Share the investigation model/provider unless explicitly overridden."""
    config = LLMConfig.from_env()
    return replace(
        config,
        api_key=os.environ.get('EXCYTIN_JUDGE_API_KEY') or config.api_key,
        base_url=os.environ.get('EXCYTIN_JUDGE_BASE_URL') or config.base_url,
        model=model or os.environ.get('EXCYTIN_JUDGE_MODEL') or config.model,
        request_timeout_seconds=float(os.environ.get('EXCYTIN_JUDGE_TIMEOUT')
                                      or config.request_timeout_seconds),
    )


def protocol_manifest(cases, *, max_steps, judge, timeout=0, max_llm_calls=0):
    source = UPSTREAM / 'evaluator.py'
    if not source.exists():
        raise FileNotFoundError(f'Official ExCyTIn evaluator is required: {source}')
    return dict(version=PROTOCOL_VERSION, max_steps=max_steps, layer='alert',
                action_budget='queries_and_submission_each_count_once',
                evaluator_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                questions_sha256=hashlib.sha256(json.dumps(
                    [(c.case_id, c.question) for c in cases], sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest(),
                judge_model=judge.model if judge else None,
                judge_base_url=judge.base_url if judge else None,
                investigation_thinking=thinking_parameters(LLMConfig.from_env()),
                judge_thinking=thinking_parameters(judge, force_thinking=False) if judge else None,
                temperature=0, ans_check_reflection=True, sol_check_reflection=True,
                step_checking=True, strict_check=False, timeout=timeout,
                max_llm_calls=max_llm_calls, sql_policy='single_read_only_statement',
                max_entry_return=15, max_str_len=100000)


def score_case(case, result, config):
    """Keep infrastructure/judge failures unscored; normal non-submission earns zero."""
    result.pop('llm_evaluation', None)
    result.pop('paper_evaluation', None)
    result.pop('judge_error', None)
    if result.get('status') in {'budget_exhausted', 'no_submission'}:
        evaluation = {'reward': 0, 'reason': 'Episode ended without submitting an answer'}
    elif result.get('status') == 'submitted':
        bridge = Bridge(None, config, max_llm_calls=40, force_thinking=False)
        try:
            with bridge:
                evaluation = paper_evaluator(bridge).checking(case.question, result['answer'])
                validate_judgement(evaluation, case.question)
        except Exception as exc:
            result['judge_error'] = f'{type(exc).__name__}: {exc}'
            result['evaluation_status'] = 'judge_error'
            return
        finally:
            # Preserve costs of failed scoring attempts when resuming.
            result.setdefault('judge_usage', []).extend(bridge.usage)
    else:
        result['evaluation_status'] = 'investigation_error'
        return
    result['paper_evaluation'] = evaluation
    result['evaluation_status'] = 'scored'
    result['llm_evaluation'] = {
        'llm_judge_answer_correct': evaluation['reward'] == 1,
        'llm_judge_reward': evaluation['reward'],
        'llm_judge_answer_analysis': evaluation.get('check_ans_reflection', evaluation.get('reason', '')),
    }
