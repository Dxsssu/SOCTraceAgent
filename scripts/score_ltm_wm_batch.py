"""Post-score saved investigations with official ExCyTIn prompts and reward."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time

from src.agent.llm import LLMClient, LLMConfig
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.benchmarks.excytin_bench.official_scoring import SDKEvaluator, SOURCE, UTILS
from scripts.run_ltm_wm_batch import ROOT, read_json, write_json


def score_one(case, record, output, config, signature):
    calls = []
    client = None
    def request(messages, json_mode):
        for attempt in range(3):
            entry = {'stage': 'steps' if json_mode else 'answer', 'attempt': attempt+1}
            try:
                kwargs = {'model': config.model, 'messages': messages}
                if json_mode: kwargs['response_format'] = {'type':'json_object'}
                response = client.client.chat.completions.create(**kwargs)
                entry.update(usage=response.usage.model_dump() if response.usage else {},
                    response=response.choices[0].message.content,
                    finish_reason=response.choices[0].finish_reason)
                return response.choices[0].message.content
            except Exception as exc:
                entry['error'] = f'{type(exc).__name__}: {exc}'
                if attempt == 2 or not ExcytinBenchWorkflow._retryable_api_error(exc): raise
                time.sleep(random.uniform(5,10))
            finally:
                calls.append(entry)
    result = dict(case_id=case.case_id, signature=signature, status='scored')
    try:
        # No submitted answer earns no credit; a diagnostic failure is retained.
        if record['status'] != 'completed' and not record.get('submitted_answer'):
            result.update(reward=0, success=False, evaluation={'reason':'run_failed_without_submission'})
        else:
            client = LLMClient(config)
            evaluation = SDKEvaluator(request).checking(case.question, record.get('submitted_answer',''))
            result.update(reward=evaluation['reward'], success=evaluation['reward']==1, evaluation=evaluation)
    except Exception as exc:
        result.update(status='judge_failed', reward=None, success=None, error=f'{type(exc).__name__}: {exc}')
    finally:
        if client: client.client.close()
    result['calls'] = calls
    result['tokens'] = sum((c.get('usage') or {}).get('total_tokens',0) for c in calls)
    result['usage_missing_calls'] = sum(not c.get('usage') for c in calls)
    write_json(output/'cases'/(case.case_id+'.json'), result)
    return result


def metrics(records, expected):
    scored = [r for r in records.values() if r.get('status')=='scored']
    success = sum(r['success'] for r in scored)
    rewards = sum(r['reward'] for r in scored)
    complete = len(scored)==expected
    return dict(expected=expected, scored=len(scored), judge_failed=sum(r.get('status')=='judge_failed' for r in records.values()),
        pending=expected-len(records), success_count=success,
        acc=success/expected if complete else None, mean_reward=rewards/expected if complete else None,
        acc_scored=success/len(scored) if scored else None,
        mean_reward_scored=rewards/len(scored) if scored else None,
        evaluation_tokens=sum(r.get('tokens',0) for r in records.values()),
        note='ACC = fraction with reward == 1. Incomplete full-set metrics remain null; judge failures are not zero scores.')



def format_duration(seconds):
    if seconds is None:
        return "--:--:--"
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}"


class ScoreProgress:
    def __init__(self, total, skipped, scheduled, stream=None):
        self.total, self.skipped, self.scheduled = total, skipped, scheduled
        self.stream = stream if stream is not None else sys.stderr
        self.started = time.monotonic()
        self.last_print = 0
        self.last_done = -1
        self.width = 0

    def update(self, done, scored, failed, last="", final=False):
        now = time.monotonic()
        tty = self.stream.isatty()
        if not tty and not final and done == self.last_done and now-self.last_print < 15:
            return
        elapsed = now-self.started
        eta = 0 if done == self.scheduled else elapsed/done*(self.scheduled-done) if done else None
        handled = self.skipped+done
        fraction = handled/self.total if self.total else 1
        filled = int(24*fraction)
        line = (f"评分 [{'#'*filled}{'-'*(24-filled)}] {handled}/{self.total} {fraction:.1%}"
                f" | 本轮 {done}/{self.scheduled} 跳过 {self.skipped}"
                f" | 已评分 {scored} 失败 {failed} 待调查 {self.total-self.skipped-self.scheduled}"
                f" | 耗时 {format_duration(elapsed)} ETA {format_duration(eta)}")
        if last:
            line += f" | 最近 {last}"
        if tty:
            self.stream.write('\r'+line.ljust(self.width)+('\n' if final else ''))
            self.width = max(self.width, len(line))
        else:
            self.stream.write(line+'\n')
        self.stream.flush()
        self.last_print, self.last_done = now, done


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output',type=Path)
    p.add_argument('--workers',type=int,default=20)
    p.add_argument('--judge-provider',choices=['fancybag','paratera','xty'],default='fancybag')
    p.add_argument('--judge-model',default='deepseek-v4.1-flash')
    p.add_argument('--timeout',type=float,default=120)
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    if args.workers<1 or args.timeout<=0:p.error('workers and timeout must be positive')
    os.chdir(ROOT)
    source=args.input.resolve();output=args.output.resolve() if args.output else source/'official_scores'
    cases=load_test_cases();ready=[]
    for c in cases:
        r=read_json(source/'cases'/(c.case_id+'.json'))
        if r and r.get('status') in {'completed','failed'}:ready.append((c,r))
    if args.dry_run:
        print(f'Test cases={len(cases)}, saved results={len(ready)}, judge={args.judge_provider}/{args.judge_model}, workers={args.workers}');return
    output.mkdir(parents=True,exist_ok=True)
    with (output/'.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:p.error('Score directory is already in use')
        settings=dict(provider=args.judge_provider,model=args.judge_model,strict_check=False,
            ans_check_reflection=True,sol_check_reflection=True,step_checking=True,
            official_source_sha256=hashlib.sha256(SOURCE.read_bytes()+UTILS.read_bytes()).hexdigest())
        prior=read_json(output/'manifest.json')
        if prior and prior!=settings:p.error('Judge settings changed; select a new --output directory')
        write_json(output/'manifest.json',settings)
        os.environ['SOCAGENT_LLM_PROVIDER']=args.judge_provider
        config=replace(LLMConfig.from_env(),model=args.judge_model,max_retries=0,request_timeout_seconds=args.timeout)
        records={};pending=[]
        for c,r in ready:
            sig=hashlib.sha256(json.dumps({'settings':settings,'question':c.question,'record':r},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            cached=read_json(output/'cases'/(c.case_id+'.json'))
            if cached and cached.get('signature')==sig and cached.get('status')=='scored':records[c.case_id]=cached
            else:pending.append((c,r,sig))
        def save():
            m=metrics(records,len(cases));write_json(output/'summary.json',m)
            text=['# ExCyTIn 官方规则评分','',f'评分模型：{args.judge_provider}/{args.judge_model}',
                f'已评分 {m["scored"]}/{len(cases)}；评分失败 {m["judge_failed"]}。',
                f'ACC（全量）：{m["acc"]}；平均 Reward（全量）：{m["mean_reward"]}',
                f'已评分子集 ACC：{m["acc_scored"]}；平均 Reward：{m["mean_reward_scored"]}',
                f'评分 token：{m["evaluation_tokens"]}（独立于调查 token）。','',
                '直接复用本地官方 evaluator 的提示词、反思和 reward 计算；SDK 替代 AutoGen/Azure 传输。当前评分模型见上方配置；默认使用 Fancybag deepseek-v4.1-flash，区别于官方实验默认 GPT-4o，因此是官方规则下的 DeepSeek 评分，不是相同裁判模型的论文分数。',
                '只评最终提交文本，不把 ITT 或 SQL 轨迹拼入答案。无提交的运行失败记零分；评分接口失败待重试，不记零分。']
            (output/'summary.md').write_text('\n'.join(text)+'\n')
            return m
        m=save()
        progress=ScoreProgress(len(cases), len(records), len(pending))
        done_count=0;last=""
        progress.update(0,m['scored'],m['judge_failed'])
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures={pool.submit(score_one,c,r,output,config,sig) for c,r,sig in pending}
                while futures:
                    finished,futures=wait(futures,timeout=1,return_when=FIRST_COMPLETED)
                    for future in finished:
                        result=future.result();records[result['case_id']]=result;m=save()
                        done_count+=1
                        last=f"{result['case_id']} {result['status']} reward={result['reward']}"
                    progress.update(done_count,m['scored'],m['judge_failed'],last)
        finally:
            progress.update(done_count,m['scored'],m['judge_failed'],last,final=True)
        print('Report: '+str(output/'summary.md'))


if __name__=='__main__':main()
