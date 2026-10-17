"""Illustrative simulation only; not measured MemTrace benchmark performance.
Run: uv run --with matplotlib --with numpy python <this file>
"""
import csv,json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager,ticker
ROOT=Path(__file__).resolve().parents[4]
OUT=ROOT/'docs/figures'
rows=list(csv.DictReader((OUT/'baseline_success_cost_cases.csv').open()))
names=['OpenCode','Pi','Base','Strategy','ReAct','ExpeL','MemTrace']
data={n:{'sql':[],'tokens':[]} for n in names}
for r in rows:
 data[r['agent']]['sql'].append(int(r['sql_queries']))
 if r['total_tokens']: data[r['agent']]['tokens'].append(int(r['total_tokens']))
rng=np.random.default_rng(20261017)
synthetic=[]
for i,r in enumerate(r for r in rows if r['agent']=='ExpeL'):
 original=int(r['sql_queries'])
 # Randomly retain each query beyond the mandatory first query (p=0.65..0.85).
 sql=1+int(rng.binomial(original-1,rng.uniform(.65,.85)))
 tokens=int(round(int(r['total_tokens'])*rng.uniform(.84,.94))) if r['total_tokens'] else None
 data['MemTrace']['sql'].append(sql)
 if tokens is not None:data['MemTrace']['tokens'].append(tokens)
 synthetic.append(dict(simulation_id=f'sim-{i+1:04d}',data_type='SIMULATED',reference_case_id=r['case_id'],sql_queries=sql,total_tokens=tokens if tokens is not None else ''))
with (OUT/'simulated_memtrace_cost_cases.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(synthetic[0]));w.writeheader();w.writerows(synthetic)
meta={'data_type':'Mixed measured baselines and explicitly simulated MemTrace; not experimental evidence for MemTrace.','seed':20261017,'reference':'baseline_success_cost_cases.csv: ExpeL successful cases','sql_rule':'1 + Binomial(reference_sql - 1, Uniform(0.65, 0.85))','tokens_rule':'round(reference_total_tokens * Uniform(0.84, 0.94)); missing usage stays missing','stats':{n:{m:{'n':len(v),'mean':float(np.mean(v)),'median':float(np.median(v))} for m,v in d.items()} for n,d in data.items()}}
(OUT/'simulated_memtrace_cost_metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
p=Path('/System/Library/Fonts/Supplemental/Times New Roman.ttf')
if p.exists():font_manager.fontManager.addfont(str(p))
plt.rcParams.update({'font.family':'serif','font.serif':['Times New Roman','DejaVu Serif'],'font.size':11,'axes.labelsize':12,'xtick.labelsize':10,'ytick.labelsize':10,'axes.linewidth':.8,'pdf.fonttype':42,'axes.facecolor':'#FAFAFA'})
colors=['#EE7C68','#F6CE87','#86BDC7','#758FB3','#B5A0C7','#9DBB9A','#D5A47B']
def panel(ax,m):
 b=ax.boxplot([data[n][m] for n in names],patch_artist=True,widths=.55,whis=1.5,medianprops={'color':'#252525','linewidth':1.35},boxprops={'edgecolor':'#454545','linewidth':.85},whiskerprops={'color':'#454545','linewidth':.85},capprops={'color':'#454545','linewidth':.85},flierprops={'marker':'o','markersize':2.2,'markerfacecolor':'none','markeredgecolor':'#737373','markeredgewidth':.45,'alpha':.55})
 for box,color in zip(b['boxes'],colors):box.set_facecolor(color)
 b['boxes'][-1].set_hatch('//')
 ax.set_xticks(range(1,8),names)
 ax.set_ylabel('SQL queries per case' if m=='sql' else 'Total tokens per case')
 ax.grid(axis='y',color='#C7C7C7',linewidth=.65);ax.set_axisbelow(True)
 if m=='tokens':
  ax.set_yscale('log');ax.set_ylim(700,2e6);ax.yaxis.set_major_formatter(ticker.LogFormatterMathtext());ax.yaxis.set_minor_locator(ticker.NullLocator())
 else:ax.set_ylim(-.8,25.5);ax.yaxis.set_major_locator(ticker.MultipleLocator(5))
 for s in ax.spines.values():s.set_color('#343434')
for m in ['sql','tokens']:
 fig,ax=plt.subplots(figsize=(7.4,4.1));panel(ax,m)
 fig.subplots_adjust(left=.12,right=.985,bottom=.12,top=.96)
 for ext in ['png','pdf']:fig.savefig(OUT/f'cost_with_simulated_memtrace_{m}.{ext}',dpi=300)
 plt.close(fig)
print(json.dumps(meta['stats']['MemTrace']))
