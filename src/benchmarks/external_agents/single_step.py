"""Enforce the environment/agent boundary independently of provider stop support."""
from __future__ import annotations

import re

REVISION = 'single-step-v2'
INSTRUCTION = (
    'Return exactly one Thought and one Action: execute[SQL] or submit[answer]. '
    'Do not generate Observation or subsequent turns. Wait for the real database result. '
    'You must execute a successful database query before submitting an answer.'
)
ACTION = re.compile(r'(?im)^\s*(?:\*\*)?Action\s*:(?:\*\*)?[ \t]*')
OBSERVATION = re.compile(r'(?im)^\s*(?:\*\*)?Observation\s*:')


def parse_first_action(response: str):
    """Return the first complete action, never a later submission or invented logs."""
    marker = ACTION.search(response)
    observation = OBSERVATION.search(response)
    if marker is None or (observation and observation.start() < marker.start()):
        raise ValueError('Missing action before observation')
    operation = re.match(r'(execute|submit)\s*\[', response[marker.end():], re.I)
    if operation is None:
        raise ValueError('Invalid first action')
    kind = operation.group(1).lower()
    start = marker.end() + operation.end()
    depth, quote, i = 1, None, start
    while i < len(response):
        c = response[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                if i + 1 < len(response) and response[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif kind == 'execute' and c in "'\"`":
            quote = c
        elif c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                content = response[start:i].strip()
                if not content:
                    raise ValueError('Empty action')
                prefix = response[:marker.start()].strip()
                clean = f'{prefix}\nAction: {kind}[{content}]'.strip()
                return content, kind == 'submit', clean, len(response[i+1:].strip())
        # An incomplete first action must never swallow a fabricated later turn.
        if quote is None and c == '\n' and re.match(
            r'\s*(?:\*\*)?(?:Observation|Thought|Action)\s*:', response[i+1:], re.I
        ):
            raise ValueError('Incomplete action before next turn')
        i += 1
    raise ValueError('Unclosed action')


def act(agent, observation, *, can_submit):
    agent._add_message(observation, role='user')
    if not hasattr(agent, 'boundary_events'):
        agent.boundary_events = []
    for attempt in range(2):
        messages = agent.messages + [{'role': 'system', 'content': INSTRUCTION}]
        response = agent._call_llm(messages)
        try:
            content, submit, clean, discarded = parse_first_action(response)
            if submit and not can_submit:
                raise ValueError('Submission before a successful SQL query')
        except ValueError as exc:
            agent.boundary_events.append({'step': agent.step_count + 1,
                                          'attempt': attempt + 1, 'rejected': str(exc)})
            if attempt:
                raise ValueError(f'Action protocol failed after one repair: {exc}') from exc
            continue
        agent.boundary_events.append({'step': agent.step_count + 1,
                                      'attempt': attempt + 1, 'discarded_characters': discarded})
        # Only the action actually executed enters subsequent model context.
        agent._add_message(clean, role='assistant')
        agent.step_count += 1
        return content, submit
