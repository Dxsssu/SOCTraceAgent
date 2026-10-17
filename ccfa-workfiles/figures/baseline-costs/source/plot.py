"""Reproduce successful-case cost boxplots from immutable benchmark summaries.
Run: uv run --with matplotlib --with numpy python <this file>
Success = evaluation_status=scored and llm_judge_answer_correct=True.
Tokens = sum(prompt_tokens + completion_tokens) over all investigation usage entries.
Cached/reasoning tokens are already included; do not add them again.
Judge, offline training and post-terminal usage are excluded.
"""
import csv
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager, ticker

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'docs/figures'
OUT.mkdir(parents=True, exist_ok=True)
SOURCES = {
 'OpenCode': 'opencode/DeepSeek-V4.1-Flash',
 'Pi': 'pi/DeepSeek-V4.1-Flash',
 'Base': 'paper_baseline/DeepSeek-V4.1-Flash',
 'Strategy': 'prompt_sauce/DeepSeek-V4.1-Flash',
 'ReAct': 'react/fancybag-deepseek-v4.1-flash/single-step-v2/full589-steps15',
 'ExpeL': 'expel/fancybag-deepseek-v4.1-flash/single-step-v2/full589-steps15',
}
COLORS = ['#EE7C68', '#F6CE87', '#86BDC7', '#758FB3', '#B5A0C7', '#9DBB9A']
font_path = Path('/System/Library/Fonts/Supplemental/Times New Roman.ttf')
if font_path.exists(): font_manager.fontManager.addfont(str(font_path))
plt.rcParams.update({'font.family':'serif','font.serif':['Times New Roman','DejaVu Serif'],
 'font.size':11,'axes.labelsize':12,'xtick.labelsize':11,'ytick.labelsize':10,
 'axes.linewidth':.8,'pdf.fonttype':42,'ps.fonttype':42,'svg.fonttype':'none',
 'figure.facecolor':'white','axes.facecolor':'#FAFAFA'})
records=[]; stats=[]; data={}; metadata={}
for name, rel in SOURCES.items():
 path=ROOT/'results'/rel/'summary.json'
 summary=json.loads(path.read_text(encoding='utf-8'))
 rows=[r for r in summary['results'] if r.get('evaluation_status')=='scored' and r.get('llm_evaluation',{}).get('llm_judge_answer_correct') is True and not r.get('judge_error')]
 expected=next(iter(summary['overall'].values()))['correct']
 assert len(rows)==expected,(name,len(rows),expected)
 data[name]={'sql':[],'tokens':[]}
 partial=0
 for r in rows:
  queries=sum(a.get('type')=='execute_sql' for a in r.get('actions',[]))
  usage=r.get('usage',[])
  valid=[u for u in usage if isinstance(u.get('prompt_tokens'),(int,float)) and isinstance(u.get('completion_tokens'),(int,float))]
  inp=sum(u['prompt_tokens'] for u in valid); output=sum(u['completion_tokens'] for u in valid)
  missing=len(valid)!=len(usage) or not valid or r.get('llm_calls',len(usage))!=len(usage)
  tokens=inp+output if not missing else None
  partial+=missing
  data[name]['sql'].append(queries)
  if tokens is not None: data[name]['tokens'].append(tokens)
  records.append(dict(agent=name,case_id=r['case_id'],status=r['status'],sql_queries=queries,recorded_input_tokens=inp if valid else '',recorded_output_tokens=output if valid else '',total_tokens=tokens if tokens is not None else '',usage_entries=len(usage),usage_entries_with_counts=len(valid),llm_calls=r.get('llm_calls'),partial_usage=missing,source=str(path.relative_to(ROOT))))
 for metric, vals in data[name].items():
  a=np.asarray(vals,dtype=float)
  stats.append(dict(agent=name,metric=metric,n=len(a),mean=float(a.mean()),median=float(np.median(a)),q1=float(np.quantile(a,.25)),q3=float(np.quantile(a,.75)),min=float(a.min()),max=float(a.max()),total=int(a.sum())))
 metadata[name]={'source':str(path.relative_to(ROOT)),'successful_cases':len(rows),'cases_with_partial_or_missing_usage':partial,'complete_token_cases':len(data[name]['tokens']),'max_steps':summary['protocol']['max_steps'],'judge_base_url':summary['protocol']['judge_base_url']}
for filename, rows in [('baseline_success_cost_cases.csv',records),('baseline_success_cost_statistics.csv',stats)]:
 with (OUT/filename).open('w',encoding='utf-8',newline='') as f:
  writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
notes={
 'success_definition':'Scored and judge-confirmed correct final answer; per-method successful subsets, not a paired/common subset.',
 'sql_definition':'All execute_sql actions within each successful case, including schema discovery and failed SQL attempts; submission excluded.',
 'token_definition':'Sum of input + output tokens over every recorded investigation model call, including repeated context. Cached and reasoning tokens are subsets, not added twice. Excludes judge, offline training, post-terminal calls and prior archived attempts.',
 'missing_usage_policy':'Cases with any missing prompt/completion counts or usage length different from llm_calls are excluded from token statistics; retained for SQL statistics. Recorded partial counts remain in audit CSV, total_tokens blank.',
 'box_definition':'Median, 25th/75th percentiles; whiskers to furthest values within 1.5 IQR; all outliers displayed. Token axis logarithmic, quartiles calculated in raw token units.',
 'method_mapping':'Strategy = Prompt-Sauce; Base/ReAct/ExpeL are ExCyTIn methods; no synthetic Ours data.',
 'comparison_limit':'Per-method successful subsets differ; ReAct/ExpeL budget 15, others 25; providers differ. This is conditional observed cost, not a controlled efficiency ranking.',
 'sources':metadata}
(OUT/'baseline_success_cost_metadata.json').write_text(json.dumps(notes,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def panel(ax,metric):
 values=[data[n][metric] for n in SOURCES]
 b=ax.boxplot(values,patch_artist=True,widths=.55,whis=1.5,showfliers=True,
  medianprops={'color':'#252525','linewidth':1.35},boxprops={'edgecolor':'#454545','linewidth':.85},
  whiskerprops={'color':'#454545','linewidth':.85},capprops={'color':'#454545','linewidth':.85},
  flierprops={'marker':'o','markersize':2.2,'markerfacecolor':'none','markeredgecolor':'#737373','markeredgewidth':.45,'alpha':.55})
 for patch,color in zip(b['boxes'],COLORS):patch.set_facecolor(color)
 ax.set_xticks(range(1,7),[f'{n}\n(n={len(data[n][metric])})' for n in SOURCES])
 ax.set_xlabel('Agent / method',labelpad=8)
 ax.set_ylabel('SQL queries per successful case' if metric=='sql' else 'Total tokens per successful case')
 ax.grid(axis='y',color='#C7C7C7',linewidth=.65);ax.set_axisbelow(True)
 ax.tick_params(axis='both',direction='out',length=3,width=.65)
 if metric=='tokens':
  ax.set_yscale('log');ax.set_ylim(700,2e6)
  ax.yaxis.set_major_locator(ticker.LogLocator(base=10));ax.yaxis.set_major_formatter(ticker.LogFormatterMathtext())
  ax.yaxis.set_minor_locator(ticker.NullLocator())
 else:
  ax.set_ylim(-.8,25.5);ax.yaxis.set_major_locator(ticker.MultipleLocator(5))
 for spine in ax.spines.values():spine.set_color('#343434')

for metric in ['sql','tokens']:
 fig,ax=plt.subplots(figsize=(6.6,3.65));panel(ax,metric)
 fig.subplots_adjust(left=.13,right=.985,bottom=.24,top=.97)
 stem='baseline_success_'+metric+'_boxplot'
 for ext in ['png','pdf']:fig.savefig(OUT/f'{stem}.{ext}',dpi=300)
 plt.close(fig)
fig,axs=plt.subplots(1,2,figsize=(12.4,3.75))
for ax,metric,subtitle in zip(axs,['sql','tokens'],['(a) SQL interactions','(b) Cumulative token consumption']):
 panel(ax,metric);ax.text(.5,-.35,subtitle,transform=ax.transAxes,ha='center',va='top',fontsize=12)
fig.subplots_adjust(left=.065,right=.99,bottom=.29,top=.97,wspace=.25)
for ext in ['png','pdf']:fig.savefig(OUT/f'baseline_success_cost_boxplots.{ext}',dpi=300)
plt.close(fig)
print(json.dumps({'stats':stats,'sources':metadata},ensure_ascii=False,indent=2))
