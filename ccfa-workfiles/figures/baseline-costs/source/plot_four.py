"""Four publication panels; MemTrace is simulated, baselines are measured.
Simulation disclosure belongs in the manuscript caption per user request.
Run: uv run --with matplotlib --with numpy python <this file>
"""
import csv,json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager,ticker
from matplotlib.patches import Patch
ROOT=Path(__file__).resolve().parents[4]; OUT=ROOT/'docs/figures'
names=['OpenCode','Pi','QwenCode','Base','Strategy','ReAct','ExpeL','MemTrace']
colors=['#EE7C68','#F6CE87','#C4C7CA','#86BDC7','#758FB3','#B5A0C7','#9DBB9A','#D5A47B']
data={n:{'sql':[],'tokens':[]} for n in names}
for filename in ['baseline_success_cost_cases.csv','simulated_memtrace_cost_cases.csv']:
 for r in csv.DictReader((OUT/filename).open()):
  n=r.get('agent','MemTrace');data[n]['sql'].append(int(r['sql_queries']))
  if r['total_tokens']:data[n]['tokens'].append(int(r['total_tokens']))
qwen_source='results/qwen-code/fancybag-deepseek-v4.1-flash/single-step-v2/full589-steps15/summary.json'
qwen=json.loads((ROOT/qwen_source).read_text())
audit=[]
for r in qwen['results']:
 if r.get('evaluation_status')!='scored' or r.get('llm_evaluation',{}).get('llm_judge_answer_correct') is not True or r.get('judge_error'):continue
 sql=sum(a.get('type')=='execute_sql' for a in r.get('actions',[]))
 usage=r.get('usage',[])
 complete=bool(usage) and len(usage)==r.get('llm_calls',len(usage)) and all(isinstance(u.get('prompt_tokens'),(int,float)) and isinstance(u.get('completion_tokens'),(int,float)) for u in usage)
 tokens=sum(u['prompt_tokens']+u['completion_tokens'] for u in usage) if complete else None
 data['QwenCode']['sql'].append(sql)
 if tokens is not None:data['QwenCode']['tokens'].append(tokens)
 audit.append(dict(case_id=r['case_id'],sql_queries=sql,total_tokens=tokens if tokens is not None else '',complete_usage=complete))
with (OUT/'qwen_success_cost_cases.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(audit[0]));w.writeheader();w.writerows(audit)
print({m:{'n':len(v),'mean':float(np.mean(v)),'max':max(v)} for m,v in data['QwenCode'].items()})
p=Path('/System/Library/Fonts/Supplemental/Times New Roman.ttf')
if p.exists():font_manager.fontManager.addfont(str(p))
plt.rcParams.update({'font.family':'serif','font.serif':['Times New Roman','DejaVu Serif'],'font.size':9,'axes.labelsize':10,'xtick.labelsize':8,'ytick.labelsize':8,'axes.linewidth':.7,'pdf.fonttype':42,'axes.facecolor':'#FAFAFA','savefig.facecolor':'white'})
handles=[Patch(facecolor=c,edgecolor='#454545',linewidth=.6,label=n) for n,c in zip(names,colors)]
specs=[('sql_mean','sql',False,'Mean SQL interactions'),('tokens_mean','tokens',False,'Mean token consumption'),('sql_violin','sql',True,'SQL interactions'),('tokens_violin','tokens',True,'Token consumption')]
def draw(ax,spec):
 stem,m,violin,label=spec;vals=[np.asarray(data[n][m]) for n in names];x=np.arange(1,len(names)+1)
 if not violin:
  ax.bar(x,[np.mean(v) for v in vals],width=.68,color=colors,edgecolor='#454545',linewidth=.65,zorder=3)
 else:
  # For token density, estimate KDE in log10 space and display on a log axis.
  plotted=[np.log10(v) for v in vals] if m=='tokens' else vals
  parts=ax.violinplot(plotted,positions=x,widths=.79,showextrema=False,bw_method=.25,points=250)
  for body,c in zip(parts['bodies'],colors):
   body.set_facecolor(c);body.set_edgecolor('#454545');body.set_linewidth(.65);body.set_alpha(1)
   if m=='tokens':
    for path in body.get_paths():path.vertices[:,1]=10**path.vertices[:,1]
  for pos,v in zip(x,vals):
   q1,med,q3=np.percentile(v,[25,50,75]);ax.vlines(pos,q1,q3,color='#333333',lw=2,zorder=4);ax.scatter(pos,med,s=9,color='white',edgecolors='#333333',linewidths=.45,zorder=5)
 ax.set_xlim(.35,len(names)+.65);ax.set_xticks([]);ax.set_xlabel('Agent',labelpad=3);ax.set_ylabel(label,labelpad=3)
 ax.grid(axis='y',color='#BBBBBB',linewidth=.55);ax.set_axisbelow(True)
 ax.tick_params(axis='y',length=2,width=.55,pad=2)
 if m=='sql':
  ax.set_ylim(0,26 if violin else 12);ax.yaxis.set_major_locator(ticker.MultipleLocator(5 if violin else 2))
 elif violin:
  ax.set_yscale('log');ax.set_ylim(1e3,2e6);ax.yaxis.set_major_locator(ticker.LogLocator(base=10,numticks=5));ax.yaxis.set_minor_locator(ticker.NullLocator())
 else:
  ax.set_ylim(0,190000);ax.yaxis.set_major_locator(ticker.MultipleLocator(50000));ax.ticklabel_format(axis='y',style='sci',scilimits=(4,4),useMathText=True)
 for s in ax.spines.values():s.set_color('#343434')
titles=['(a) Average SQL Interactions','(b) Average Token Consumption','(c) SQL Interaction Distribution','(d) Token Consumption Distribution']
for spec,title in zip(specs,titles):
 fig,ax=plt.subplots(figsize=(2.65,2.15));draw(ax,spec);fig.subplots_adjust(left=.23,right=.98,bottom=.21,top=.91)
 fig.text(.605,.015,title,ha='center',va='bottom',fontsize=8)
 for ext in ['png','pdf']:fig.savefig(OUT/f'agent_cost_{spec[0]}.{ext}',dpi=400)
 plt.close(fig)
fig=plt.figure(figsize=(10.6,.35));fig.legend(handles=handles,loc='center',ncol=8,frameon=False,handlelength=2.2,columnspacing=1.1,fontsize=9)
for ext in ['png','pdf']:fig.savefig(OUT/f'agent_cost_legend.{ext}',dpi=400)
plt.close(fig)
# Preview only: the four deliverable panels remain separate files.
fig=plt.figure(figsize=(10.6,2.5))
for i,spec in enumerate(specs):
 ax=fig.add_axes([(i+.23)/4,.20,.75/4,.65]);draw(ax,spec)
 fig.text((i+.605)/4,.025,titles[i],ha='center',va='bottom',fontsize=8)
fig.legend(handles=handles,loc='upper center',bbox_to_anchor=(.5,.995),ncol=8,frameon=False,handlelength=2.2,columnspacing=1.1,fontsize=9)
fig.savefig(OUT/'agent_cost_four_preview.png',dpi=400)
fig.savefig(OUT/'agent_cost_four.pdf')
plt.close(fig)
(OUT/'agent_cost_four_metadata.json').write_text(json.dumps({'inputs':['baseline_success_cost_cases.csv','simulated_memtrace_cost_cases.csv'],'qwen_source':qwen_source,'sample_counts':{n:{m:len(v) for m,v in d.items()} for n,d in data.items()},'memtrace_data_type':'SIMULATED; disclosure in manuscript','panels':[s[0] for s in specs],'violin':'Gaussian KDE, bandwidth 0.25, equal maximum widths; white dot=median, thick line=IQR; token KDE estimated in log10 space; observed full ranges retained','means':{n:{m:float(np.mean(v)) for m,v in d.items()} for n,d in data.items()}},indent=2)+'\n')
