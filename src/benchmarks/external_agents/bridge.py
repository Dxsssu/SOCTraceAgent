"""Shared SQL budget and OpenAI-compatible, rate-limited model gateway."""
from __future__ import annotations
import json
import secrets
import threading
import time
import httpx
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server
from src.agent.llm import LLMConfig
from src.agent.rate_limit import get_rate_limiter
from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator


class Bridge:
    def __init__(self, executor, config: LLMConfig, max_steps=25, max_llm_calls=100):
        if max_steps < 2 or max_llm_calls < 1:
            raise ValueError("max_steps >= 2 and max_llm_calls >= 1 required")
        self.executor, self.config = executor, config
        self.max_steps, self.max_llm_calls = max_steps, max_llm_calls
        self.token = secrets.token_urlsafe(32)
        self.actions, self.usage = [], []
        self.answer = None
        self.calls = 0
        self.lock = threading.Lock()
        self.app = Flask(__name__)
        self.app.add_url_rule('/execute_sql', view_func=self.execute_sql, methods=['POST'])
        self.app.add_url_rule('/submit_answer', view_func=self.submit_answer, methods=['POST'])
        self.app.add_url_rule('/v1/chat/completions', view_func=self.chat, methods=['POST'])
        self.app.add_url_rule('/v1/models', view_func=lambda: jsonify(data=[{'id': config.model, 'object': 'model'}]))
        @self.app.before_request
        def authorize():
            if request.headers.get('Authorization') != 'Bearer ' + self.token:
                return jsonify(error='Unauthorized'), 401

    def execute_sql(self):
        with self.lock:
            if self.answer is not None:
                return jsonify(error='Answer already submitted')
            if len(self.actions) >= self.max_steps - 1:
                return jsonify(error='Query budget exhausted. Submit your best answer now.')
            sql = str(request.get_json().get('sql', ''))
            started = time.monotonic()
            try:
                validated = ReadOnlySQLValidator.validate(sql)
                observation, success, rows = self.executor.execute(validated)
            except Exception as exc:
                observation, success, rows = str(exc), False, 0
            action = dict(type='execute_sql', sql=sql, observation=observation,
                          success=success, row_count=rows, elapsed_seconds=time.monotonic()-started)
            self.actions.append(action)
            return jsonify(**action, queries_remaining=self.max_steps-1-len(self.actions))

    def submit_answer(self):
        with self.lock:
            if self.answer is not None:
                return jsonify(error='Answer already submitted')
            answer = request.get_json().get('answer')
            if not isinstance(answer, str) or not answer.strip():
                return jsonify(error='Answer must be a nonempty string')
            self.answer = answer.strip()
            self.actions.append(dict(type='submit_answer', answer=self.answer))
            return jsonify(submitted=True)

    def chat(self):
        with self.lock:
            if self.calls >= self.max_llm_calls:
                return jsonify(error={'message': 'LLM call budget exhausted'}), 429
            self.calls += 1
        body = request.get_json()
        stream = body.pop('stream', False)
        body.pop('stream_options', None)
        body['model'] = self.config.model
        body['temperature'] = 0
        body['thinking'] = {'type': 'disabled'}
        # Restrict exposed model tools to the benchmark MCP interface. Cline
        # uses a generic MCP dispatcher; Qwen may need tool_search discovery.
        if 'tools' in body:
            body['tools'] = [t for t in body['tools'] if any(
                s in t.get('function', {}).get('name', '').lower()
                for s in ('execute_sql', 'submit_answer', 'use_mcp_tool', 'tool_search'))]
            if not body['tools']:
                body.pop('tools')
                body.pop('tool_choice', None)
        limiter = get_rate_limiter()
        reservation = limiter.acquire(len(json.dumps(body).encode()) + 16384)
        usage = None
        try:
            with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
                response = client.post(self.config.base_url.rstrip('/')+'/chat/completions',
                    headers={'Authorization': 'Bearer '+self.config.api_key}, json=body)
            if response.status_code != 200:
                self.usage.append({'error_status': response.status_code})
                return jsonify(error={'message': 'Upstream model request failed', 'status': response.status_code}), 502
            result = response.json()
            usage = result.get('usage', {})
            self.usage.append(usage)
        except Exception as exc:
            self.usage.append({'error_type': type(exc).__name__})
            return jsonify(error={'message': type(exc).__name__}), 502
        finally:
            limiter.finish(reservation, usage.get('total_tokens') if usage else None)
        if not stream:
            return jsonify(result)
        # Buffer one completion, then expose valid SSE chunks to all three CLIs.
        events = []
        base = {k: result[k] for k in ('id', 'created', 'model') if k in result}
        base['object'] = 'chat.completion.chunk'
        for choice in result['choices']:
            delta = dict(choice['message'])
            for index, tool in enumerate(delta.get('tool_calls') or []):
                tool['index'] = index
            events.append({**base, 'choices': [{'index': choice['index'], 'delta': delta, 'finish_reason': None}]})
            events.append({**base, 'choices': [{'index': choice['index'], 'delta': {}, 'finish_reason': choice['finish_reason']}]})
        events.append({**base, 'choices': [], 'usage': usage})
        return Response(''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n', mimetype='text/event-stream')

    def __enter__(self):
        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
