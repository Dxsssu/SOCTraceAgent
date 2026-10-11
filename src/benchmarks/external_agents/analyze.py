"""Aggregate paired experiment outputs without changing any investigation."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import re
from pathlib import Path
import statistics
from .runner import write_json
from src.benchmarks.excytin_bench.runner import load_test_cases


def usage_total(items, prices=None):
    inp = out = cached = 0
    errors = 0
    for u in items:
        inp += int(u.get('prompt_tokens', 0))
        out += int(u.get('completion_tokens', 0))
        cached += int((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0))
        errors += int('error_status' in u or 'error_type' in u)
    return {'input': inp, 'output': out, 'cached': cached,
            'cost_cny': ((inp-cached)*prices['input'] + cached*prices['cached'] + out*prices['output'])/1e6 if prices else None,
            'api_errors': errors}


def stats(rows, expected=None):
    n = len(rows)
    expected = n if expected is None else expected
    judged = [r for r in rows if 'llm_evaluation' in r and not r.get('judge_error')]
    complete = bool(expected) and len(judged) == expected
    total_reward = sum(r['llm_evaluation'].get('llm_judge_reward', 0) for r in judged)
    correct = sum(bool(r['llm_evaluation'].get('llm_judge_answer_correct')) for r in judged)
    steps = [[a for a in r.get('actions', []) if a['type']=='execute_sql'] for r in rows]
    queries = [len(s) for s in steps]
    schema_queries = [sum(bool(re.match(r'^\s*(SHOW|DESCRIBE|DESC)\b', a['sql'], re.I))
                          or 'information_schema.' in a['sql'].lower() for a in s) for s in steps]
    sql_errors = sum(not a.get('success', False) for s in steps for a in s)
    empty = sum(a.get('success') and a.get('row_count') == 0 for s in steps for a in s)
    token = usage_total([u for r in rows for u in r.get('usage', [])])
    judge_token = usage_total([u for r in rows for u in r.get('judge_usage', [])])
    latency = [r['elapsed_seconds'] for r in rows if 'elapsed_seconds' in r]
    memory = [r.get('workflow', {}).get('memory_retrievals', []) for r in rows]
    return dict(n=n, expected=expected, judged=len(judged), scoring_complete=complete, correct=correct,
        accuracy=correct/expected if complete else None, mean_reward=total_reward/expected if complete else None,
        scored_subset_accuracy=correct/len(judged) if judged else None,
        scored_subset_mean_reward=total_reward/len(judged) if judged else None,
        submitted=sum(r.get('status') == 'submitted' for r in rows),
        exact=sum(bool(r.get('evaluation',{}).get('exact_match')) for r in rows),
        mean_queries=statistics.mean(queries) if queries else 0, queries=sum(queries),
        mean_schema_queries=statistics.mean(schema_queries) if schema_queries else 0,
        mean_data_queries=statistics.mean([n-s for n,s in zip(queries,schema_queries)]) if queries else 0,
        sql_errors=sql_errors, empty_results=empty,
        mean_seconds=statistics.mean(latency) if latency else 0,
        median_seconds=statistics.median(latency) if latency else 0,
        mean_llm_calls=statistics.mean([r.get('llm_calls',0) for r in rows]) if rows else 0,
        cases_with_episodic=sum(any(m.get('episode_ids') for m in items) for items in memory),
        episodic_retrievals=sum(bool(m.get('episode_ids')) for items in memory for m in items),
        investigation_tokens=token, judge_tokens=judge_token,
        total_cost_cny=None,
        post_terminal_tokens=usage_total([u for r in rows for u in r.get('post_terminal_usage', [])]),
        statuses=dict(Counter(r['status'] for r in rows)))


def paired(rows, agent):
    lookup = {(r['agent'],r['case_id']):r for r in rows}
    wins = losses = both = neither = 0
    for (a, cid), ours in lookup.items():
        if a != 'ours' or (agent,cid) not in lookup:
            continue
        other = lookup[(agent,cid)]
        if ('llm_evaluation' not in ours or 'llm_evaluation' not in other
                or ours.get('judge_error') or other.get('judge_error')):
            continue
        x=bool(ours['llm_evaluation'].get('llm_judge_answer_correct'))
        y=bool(other['llm_evaluation'].get('llm_judge_answer_correct'))
        wins += x and not y
        losses += y and not x
        both += x and y
        neither += not x and not y
    discordant = wins+losses
    p = min(1., 2*sum(math.comb(discordant,k) for k in range(min(wins,losses)+1))/2**discordant) if discordant else 1.
    return dict(ours_only=wins, other_only=losses, both=both, neither=neither, exact_mcnemar_p=p)


def analyze(root):
    manifest = json.loads((root/'manifest.json').read_text())
    agents = manifest['agents']
    case_ids = {c['case_id'] for c in manifest['cases']}
    rows = [json.loads(p.read_text()) for p in root.glob('*/*/result.json')]
    rows = [r for r in rows if r['agent'] in agents and r['case_id'] in case_ids]
    overall = {a:stats([r for r in rows if r['agent']==a], expected=len(case_ids)) for a in agents}
    incidents = list(dict.fromkeys(c['incident'] for c in manifest['cases']))
    per_incident = {i:{a:stats([r for r in rows if r['agent']==a and r['incident']==i],
        expected=sum(c['incident']==i for c in manifest['cases'])) for a in agents} for i in incidents}
    pairing = {a:paired(rows,a) for a in agents if a!='ours'}
    questions = {c.case_id:c.question for c in load_test_cases()}
    def path_group(row):
        path = questions[row['case_id']].get('shortest_alert_path') or []
        if not path:
            return 'unknown'
        # Upstream AlertGraph connects alerts only to entity nodes. A shared
        # entity transition A -> E -> A is one alert hop, not two alert hops.
        if len(path) % 2 == 0:
            raise ValueError('Expected an alternating alert/entity path')
        hops = (len(path)-1)//2
        return 'same_alert' if hops == 0 else ('one_hop' if hops == 1 else 'two_or_more_hops')
    difficulty = {group:{a:stats([r for r in rows if r['agent']==a and path_group(r)==group],
                        expected=sum(path_group(c)==group for c in manifest['cases']))
                        for a in agents}
                  for group in ('same_alert', 'one_hop', 'two_or_more_hops', 'unknown')}
    report = {'completed':len(rows), 'expected':len(manifest['cases'])*len(agents),
              'protocol':manifest.get('protocol', {'version': 'legacy'}),
              'scoring_complete':all(s['scoring_complete'] for s in overall.values()),
              'overall':overall, 'per_incident':per_incident, 'paired':pairing, 'path_groups':difficulty}
    write_json(root/'analysis.json', report)
    lines = [f'# {len(agents)} 种 Agent 的 ExCyTIn 测评','',
        f"测试题数：{len(manifest['cases'])}；结果数：{len(rows)}/{report['expected']}。模型：{manifest['model']}。",
        '题目和 Agent 列表以本次 manifest 为准；各 Agent 使用相同题目。',
        '', '## 总体结果','',
        f"协议：{report['protocol'].get('version')}；裁判：{report['protocol'].get('judge_model', '旧记录未声明')}。",
        '缺失调查或裁判失败时总体正确率和平均奖励为未完成，不按 0 分填补。',
        '| Agent | 记录/计划 | 已评分 | 答对 | 正确率 | 平均奖励 | 平均 SQL | 平均调查耗时/s |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for a,s in overall.items():
        accuracy = f"{s['accuracy']:.1%}" if s['accuracy'] is not None else '未完成'
        reward = f"{s['mean_reward']:.3f}" if s['mean_reward'] is not None else '未完成'
        lines.append(f"| {a} | {s['n']}/{s['expected']} | {s['judged']} | {s['correct']} | {accuracy} | {reward} | {s['mean_queries']:.2f} | {s['mean_seconds']:.1f} |")
    lines += ['', '## 各 incident 正确题数','', '| Incident | 题数 | '+' | '.join(agents)+' |', '|---|---:|'+'---:|'*len(agents)]
    for i in incidents:
        n=sum(c['incident']==i for c in manifest['cases'])
        lines.append(f'| {i} | {n} | '+' | '.join(f"{per_incident[i][a]['correct']}/{per_incident[i][a]['expected']}（已评 {per_incident[i][a]['judged']}）" for a in agents)+' |')
    lines += ['', '## 运行与 SQL 失败','', '| Agent | 无答案 | SQL 错误 | 空结果 | 状态分布 |', '|---|---:|---:|---:|---|']
    for a,s in overall.items():
        lines.append(f"| {a} | {s['n']-s['submitted']} | {s['sql_errors']} | {s['empty_results']} | {s['statuses']} |")
    lines += ['', '## 区分结构探索和数据查询','',
              '| Agent | 平均结构探索 SQL | 平均其他 SQL |', '|---|---:|---:|']
    for a,s in overall.items():
        lines.append(f"| {a} | {s['mean_schema_queries']:.2f} | {s['mean_data_queries']:.2f} |")
    lines += ['', '结构探索按 SHOW/DESCRIBE/DESC 和 information_schema 查询识别；其余归为其他 SQL。SM 预先提供结构，SQL 总数差异不能直接归因于规划效率。']
    lines += ['', '## 模型和记忆开销','',
              '| Agent | 平均模型请求数 | 输入/百万 tokens | 输出/百万 tokens | 输入缓存比例 | 触发 EM 的题数 |',
              '|---|---:|---:|---:|---:|---:|']
    for a,s in overall.items():
        u=s['investigation_tokens']
        lines.append(f"| {a} | {s['mean_llm_calls']:.1f} | {u['input']/1e6:.2f} | {u['output']/1e6:.3f} | {u['cached']/max(1,u['input']):.1%} | {s['cases_with_episodic']} |")
    lines += ['', '## 与我们框架逐题配对','', '| 对照 | 仅我们答对 | 仅对照答对 | 都答对 | 都失败 | McNemar 精确 p |', '|---|---:|---:|---:|---:|---:|']
    for a,s in pairing.items():
        lines.append(f"| {a} | {s['ours_only']} | {s['other_only']} | {s['both']} | {s['neither']} | {s['exact_mcnemar_p']:.4f} |")
    lines += ['', '## 按告警关联路径分组（答对/题数）','',
              '| 数据集路径 | '+' | '.join(agents)+' |', '|---|'+'---:|'*len(agents)]
    for group, group_agents in difficulty.items():
        lines.append('| '+group+' | '+' | '.join(f"{s['correct']}/{s['expected']}（已评 {s['judged']}）" for s in group_agents.values())+' |')
    lines += ['', '## 判读限制', '',
        '- 新协议复用官方答案复核、步骤复核和 0.4 衰减奖励；实际裁判模型与协议以 manifest 为准。旧实验仍标记 legacy。',
        '- 每次查询或提交各计一步，达到总动作上限即结束；正常无提交记 0 分，基础设施或裁判错误保留未评分。',
        '- 新协议调查耗时截止提交或动作耗尽；进程退出耗时和结束后在途请求 usage 单独记录。限流等待包含在耗时内。',
        '- 我们的框架有 SM/PM/条件触发 EM，并自行预留提交步及压缩 observation；其他对照不提供这些记忆。',
        '- Baseline 保留官方提示词；CLI 通过 MCP 工具使用原生循环。预算感知方式仍属于 Agent 实现差异。',
        '- 原生 CLI、只读 SQL 校验及缓冲 SSE 是保留的适配条件，不能视为未经修改的官方运行器。',
        '- 未配置各模型的价格，报告仅累计真实 token usage，不硬编码模型单价。',
        '- 配对统计仅包含双方均已评分的题目；p 值未校正多重比较。',
        '', '## 未答对或未评分的逐题索引', '']
    for r in rows:
        ev=r.get('llm_evaluation',{})
        if not ev.get('llm_judge_answer_correct'):
            p=f"{r['agent']}/{r['case_id']}/result.json"
            reason=r.get('judge_error') or r.get('error') or ev.get('llm_judge_answer_analysis','Missing judge')
            lines.append(f"- [{r['agent']} / {r['case_id']}]({p})：{str(reason).replace(chr(10),' ')[:500]}")
    (root/'analysis.md').write_text('\n'.join(lines)+'\n')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    result=analyze(parser.parse_args().root)
    print(json.dumps({k:result[k] for k in ('completed','expected','overall')},ensure_ascii=False,indent=2))
