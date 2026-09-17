"""Aggregate paired experiment outputs without changing any investigation."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import re
from pathlib import Path
import statistics
from .batch import ALL_AGENTS
from .runner import write_json
from src.benchmarks.excytin_bench.runner import load_test_cases


def usage_total(items):
    inp = out = cached = 0
    errors = 0
    for u in items:
        inp += int(u.get('prompt_tokens', 0))
        out += int(u.get('completion_tokens', 0))
        cached += int((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0))
        errors += int('error_status' in u or 'error_type' in u)
    return {'input': inp, 'output': out, 'cached': cached,
            'cost_cny': ((inp-cached)*2 + cached*0.04 + out*8)/1e6,
            'api_errors': errors}


def stats(rows):
    n = len(rows)
    judged = [r for r in rows if 'llm_evaluation' in r]
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
    return dict(n=n, judged=len(judged), correct=correct,
        accuracy=correct/n if n else 0, mean_reward=sum(r['llm_evaluation'].get('llm_judge_reward',0) for r in judged)/n if n else 0,
        submitted=sum(bool(r.get('answer')) for r in rows),
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
        total_cost_cny=token['cost_cny']+judge_token['cost_cny'],
        statuses=dict(Counter(r['status'] for r in rows)))


def paired(rows, agent):
    lookup = {(r['agent'],r['case_id']):r for r in rows}
    wins = losses = both = neither = 0
    for (a, cid), ours in lookup.items():
        if a != 'ours' or (agent,cid) not in lookup:
            continue
        other = lookup[(agent,cid)]
        if 'llm_evaluation' not in ours or 'llm_evaluation' not in other:
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
    rows = [json.loads(p.read_text()) for p in root.glob('*/*/result.json')]
    overall = {a:stats([r for r in rows if r['agent']==a]) for a in ALL_AGENTS}
    incidents = list(dict.fromkeys(c['incident'] for c in manifest['cases']))
    per_incident = {i:{a:stats([r for r in rows if r['agent']==a and r['incident']==i]) for a in ALL_AGENTS} for i in incidents}
    pairing = {a:paired(rows,a) for a in ALL_AGENTS if a!='ours'}
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
    difficulty = {group:{a:stats([r for r in rows if r['agent']==a and path_group(r)==group])
                        for a in ALL_AGENTS}
                  for group in ('same_alert', 'one_hop', 'two_or_more_hops', 'unknown')}
    report = {'completed':len(rows), 'expected':len(manifest['cases'])*len(ALL_AGENTS),
              'overall':overall, 'per_incident':per_incident, 'paired':pairing, 'path_groups':difficulty}
    write_json(root/'analysis.json', report)
    lines = ['# 五种 Agent 的 ExCyTIn 测评','',
        f"测试题数：{len(manifest['cases'])}；结果数：{len(rows)}/{report['expected']}。模型：{manifest['model']}。",
        '测试集固定抽样，7 个 incident 各 15 题，incident_38 全部 11 题；五者使用相同题目。',
        '', '## 总体结果','',
        '| Agent | 完成记录 | 答对 | 正确率 | 平均奖励 | 平均 SQL | 平均耗时/s | 调查成本/元 | 含裁判成本/元 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for a,s in overall.items():
        lines.append(f"| {a} | {s['n']} | {s['correct']} | {s['accuracy']:.1%} | {s['mean_reward']:.3f} | {s['mean_queries']:.2f} | {s['mean_seconds']:.1f} | {s['investigation_tokens']['cost_cny']:.2f} | {s['total_cost_cny']:.2f} |")
    lines += ['', '## 各 incident 正确题数','', '| Incident | 题数 | '+' | '.join(ALL_AGENTS)+' |', '|---|---:|'+'---:|'*len(ALL_AGENTS)]
    for i in incidents:
        n=sum(c['incident']==i for c in manifest['cases'])
        lines.append(f'| {i} | {n} | '+' | '.join(f"{per_incident[i][a]['correct']}/{per_incident[i][a]['n']}" for a in ALL_AGENTS)+' |')
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
              '| 数据集路径 | '+' | '.join(ALL_AGENTS)+' |', '|---|'+'---:|'*len(ALL_AGENTS)]
    for group, agents in difficulty.items():
        lines.append('| '+group+' | '+' | '.join(f"{s['correct']}/{s['n']}" for s in agents.values())+' |')
    lines += ['', '## 判读限制','',
        '- 正确率以本地裁判的最终答案判断为准；平均奖励包含 0.4、0.16 等步骤部分分，不能等同正确率。无答案按 0 分。裁判异常须补评，不能当作答案错误。',
        '- 每题最多 24 次 SQL 加 1 次提交；并发数 3，所有模型调用共享限流，耗时包含等待，非纯推理速度。Python 框架在步骤间检查 600 秒超时，进行中的请求可能超出。',
        '- 我们的框架有 SM/PM/条件触发 EM，其他四者没有；这是整套系统对比，不能单独归因于 TTT 或多智能体。',
        '- 原生 ours workflow 还会将单次 observation 压缩到 20,000 字符；CLI 使用公共 SQL 边界的返回值。这种上下文处理差异属于当前实现，可能影响答案完整性。',
        '- Baseline 保留原始提示词（未新增显式剩余预算提示）；五者外层查询上限相同，但预算感知与停止机制不同。',
        '- 路径分组按原仓库二部图结构解释：告警→实体→告警为一次跨告警关联，即 (len(shortest_alert_path)-1)/2；不保证等于实际 SQL 调查难度。',
        '- 论文框架是原仓库 BaselineAgent 的原生提示词与循环，替换模型传输；统一使用本项目 MySQL 边界和本地裁判，不是论文原模型的官方成绩。',
        '- CLI 模型工具集被限制到 MCP 查询、提交和必要分发；模型代理缓冲 SSE，影响首 token 时延。',
        '- 单题单次运行，每个 incident 样本较少；配对 p 值为探索性、未校正多重比较，不能据此宣称广泛领先。',
        '- 成本使用用户提供价格：输入 2 元/百万、缓存输入 0.04 元/百万、输出 8 元/百万；以接口 usage 计算。无 usage 的失败请求及未知供应商计费不含在内。',
        '', '## 未答对的逐题索引','']
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
