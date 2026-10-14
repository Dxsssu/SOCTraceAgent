"""Adapt released ExpeL artifacts; retain upstream prompts and top-1 recall."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    load_jsonl, load_train_questions, map_trajectories_to_train,
)


def prepare_assets(upstream: Path, cache: Path, train: Path):
    root = upstream/'secgym/agents'
    source = root/'expel_train/corrects.jsonl'
    insights = root/'expel_train/insights.json'
    rules = json.loads(insights.read_text())
    if not rules or not all(isinstance(s, str) and s.strip() for s in rules):
        raise ValueError('ExpeL requires a nonempty list of insight strings')
    mapped = map_trajectories_to_train(load_jsonl(source), load_train_questions(train))
    if not mapped:
        raise ValueError('ExpeL requires training trajectories')
    rows, fixed = [], 0
    for trajectory, task, _ in mapped:
        question = task['payload']
        key = {k: question[k] for k in ('context', 'question')}
        fixed += key != trajectory['key']
        rows.append({'key': key, 'value': trajectory['value']})
    content = ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows)
    digest = hashlib.sha256(content.encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    aligned = cache/f'corrects-{digest}.jsonl'
    if not aligned.exists():
        aligned.write_text(content)
    paths = [root/'expel_agent.py', root/'agent_utils.py',
             root/'expel_train/experience_recall.py', source, insights]
    metadata = {
        'sources': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        'aligned_experiences_sha256': digest,
        'training_questions_sha256': hashlib.sha256(json.dumps(load_train_questions(train), sort_keys=True).encode()).hexdigest(),
        'trajectory_count': len(rows), 'realigned_keys': fixed, 'insight_count': len(rules),
        'embedding_model': 'sentence-transformers/all-mpnet-base-v2',
        'retrieval': 'context + newline + question; cosine top-1; upstream formatting',
        'insight_origin': 'released upstream insights; not retrained with Fancybag',
    }
    return insights, aligned, metadata


def configured_class(insights: Path, experiences: Path):
    # Standard HTTP download works on networks where the Xet transport stalls.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from .batch import paper_agent_class
    original = paper_agent_class('expel_agent', 'ExpelAgent')
    module = sys.modules[original.__module__]
    # One immutable corpus/index per run, shared by all case instances.
    from secgym.agents.expel_train.experience_recall import ExperiencePool
    pool = ExperiencePool(correct_trajectories_path=str(experiences), embed_type='both')
    lock = threading.Lock()

    class SharedPool:
        def get_demonstrations(self, query, k):
            with lock:
                return pool.get_demonstrations(query, k)

    shared = SharedPool()
    module.ExperiencePool = lambda **kwargs: shared

    class ConfiguredExpel(original):
        def __init__(self, **kwargs):
            super().__init__(insight_path=str(insights), experience_path=str(experiences), **kwargs)

    return ConfiguredExpel
