#!/usr/bin/env python3
"""Render README figures directly from checked-in measurements (no model calls)."""
import json
import os
from pathlib import Path
os.environ.setdefault('MPLCONFIGDIR', '/tmp/memweft-mpl')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/assets'
COLORS=['#94a3b8','#087f8c']


def style(ax):
    for edge in ('top','right','left'):ax.spines[edge].set_visible(False)
    ax.spines['bottom'].set_color('#cbd5e1')
    ax.tick_params(length=0,labelcolor='#334155',pad=8)
    ax.set_axisbelow(True)
    ax.grid(axis='y',color='#e2e8f0',linewidth=.8)


def save(fig,name):
    svg = OUT/f'{name}.svg'
    fig.savefig(svg,facecolor='white',metadata={'Date':None})
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    fig.savefig(OUT/f'{name}.png',facecolor='white',dpi=160)
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'svg.hashsalt':'memweft-readme-v1'})
    r=json.loads((ROOT/'evals/reports/2026-09-22-wal-coordination.json').read_text())
    before,after=r['before']['result'],r['after']['result']
    fig,axes=plt.subplots(1,3,figsize=(12,4.3))
    fig.subplots_adjust(left=.065,right=.97,bottom=.24,top=.72,wspace=.4)
    fig.text(.035,.94,'One million facts · coordinated SQLite maintenance',fontsize=17,weight='bold',color='#0f172a')
    fig.text(.035,.865,'Same runner · 8 readers + 1 writer · 5 min/run · 1,000 reads/s + 50 writes/s offered',fontsize=10,color='#475569')
    series=[('Write p99','Milliseconds',[x['writer']['latency']['p99_ms'] for x in (before,after)]),
            ('Query p95','Milliseconds',[x['query_latency']['p95_ms'] for x in (before,after)]),
            ('Observed WAL peak','MiB',[x['writer_wal_max_bytes']/2**20 for x in (before,after)])]
    for ax,(title,unit,values) in zip(axes,series):
        style(ax);bars=ax.bar(['Before','Coordinated'],values,color=COLORS,width=.52)
        ax.set_title(title,loc='left',weight='bold',pad=16,color='#0f172a')
        ax.set_ylabel(unit,color='#475569');ax.set_ylim(0,max(values)*1.32)
        ax.bar_label(bars,labels=[f'{v:.2f}' for v in values],padding=7,color='#0f172a',weight='bold')
    axes[2].axhline(16,color='#c2410c',linestyle='--',linewidth=1.2)
    axes[2].text(.02,18.5,'16 MiB soft threshold',color='#9a3412',fontsize=8,transform=axes[2].get_yaxis_transform())
    fig.text(.035,.105,'Completed writes: 14,109 / 15,000 → 14,942 / 15,000. Both new runs recorded zero successful WAL truncations.',fontsize=9,color='#334155')
    fig.text(.035,.045,'Local sequential runs; not a production SLO or a hard WAL size bound. Source: 2026-09-22-wal-coordination.json',fontsize=8.5,color='#64748b')
    save(fig,'maintenance-performance')
    q=json.loads((ROOT/'evals/reports/2026-09-22-wal-and-agent.json').read_text())['query_comparison']['rows']
    rows=[x for x in q if x['profile']=='million']
    labels={'rare':'Rare term','frequent':'Frequent term','mixed_terms':'Mixed terms','absent':'No matching term',
            'empty':'No query','value_common':'Common value term','fallback':'Difficult two-term query'}
    fig,ax=plt.subplots(figsize=(12,4.8));fig.subplots_adjust(left=.23,right=.91,bottom=.22,top=.76)
    fig.text(.035,.94,'Retrieval latency depends on the query',fontsize=17,weight='bold',color='#0f172a')
    fig.text(.035,.865,'1,000,000 facts · full SDK context query · 30 timed samples per query · p95, logarithmic axis',fontsize=10,color='#475569')
    y=list(range(len(rows)));values=[r['after_p95_ms'] for r in rows]
    ax.scatter(values,y,s=80,c=['#c2410c' if r['name']=='fallback' else '#087f8c' for r in rows],zorder=3)
    for pos,value in zip(y,values):ax.text(value*1.13,pos,f'{value:.2f} ms',va='center',fontsize=10,color='#0f172a')
    ax.set_yticks(y,[labels[r['name']] for r in rows]);ax.invert_yaxis()
    ax.set_xscale('log');ax.set_xlim(.7,900);ax.set_xticks([1,10,100]);ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.set_xlabel('Milliseconds — lower is better',labelpad=10,color='#475569')
    for edge in ('top','right','left'):ax.spines[edge].set_visible(False)
    ax.spines['bottom'].set_color('#cbd5e1');ax.tick_params(length=0,pad=8,labelcolor='#334155');ax.grid(axis='x',which='major',color='#e2e8f0')
    fig.text(.035,.065,'The difficult query remains much slower; fast-path timings are not a guarantee for all queries or larger datasets.',fontsize=9,color='#475569')
    fig.text(.035,.025,'Source: 2026-09-22-wal-and-agent.json · SQLite 3.51.3 / exact lexical retrieval',fontsize=8.5,color='#64748b')
    save(fig,'retrieval-performance')
    print('Rendered maintenance-performance and retrieval-performance as SVG + PNG.')


if __name__=='__main__':main()
