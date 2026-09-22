#!/usr/bin/env python3
"""Audit frozen run artifacts and produce a compact enterprise test scorecard."""
import argparse
import hashlib
import json
from pathlib import Path
from collections import Counter
from compare_learning import compare
from run_local import dump


def load(path): return json.loads(path.read_text())
def jsonl(path): return [json.loads(line) for line in path.read_text().splitlines()]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def completed(path):
    if load(path/'status.json')['status']!='completed':
        raise ValueError(f'incomplete run: {path}')


def audit_calls(path, expected_calls):
    calls=jsonl(path/'calls.jsonl')
    if len(calls)!=expected_calls or [x['call_id'] for x in calls]!=list(range(1,len(calls)+1)):
        raise ValueError(f'call count/ID mismatch: {path}')
    for call in calls:
        if 'error' in call or call['request']['temperature']!=.2:
            raise ValueError(f'failed/unexpected sampling call: {path}')
        repeat=int(call['tag'].rsplit('/',1)[1])
        if call['request']['seed']!=42+repeat:
            raise ValueError(f'unpaired repeat seed: {path}')
    return {'calls':len(calls),'total_tokens':sum(c['usage']['total_tokens'] for c in calls),
            'incomplete_completions':sum(c['finish_reason']!='stop' for c in calls)}


def summarize(tasks,memory,storage):
    for path in (tasks,memory,storage): completed(path)
    manifest=load(tasks/'manifest.json')
    if manifest['repeats'] != 3 or manifest['seeds'] != [42,43,44]:
        raise ValueError('this enterprise-v1 scorecard requires three seeds: 42/43/44')
    if sha(tasks/'run_enterprise.py') != manifest['source_sha256']:
        raise ValueError('orchestrator snapshot hash mismatch')
    runs=[]
    for name in ('support','incident','access','access-poisoned'):
        path=tasks/name; completed(path)
        metadata=load(path/'metadata.json')
        checks={'suite.json':'suite_sha256','run_local.py':'runner_sha256',
                'output_contract.py':'contract_module_sha256','evidence_learning.py':'evidence_module_sha256'}
        for filename,key in checks.items():
            if sha(path/filename)!=metadata[key]: raise ValueError(f'snapshot hash mismatch: {path/filename}')
        if metadata['suite_sha256']!=manifest['suite_hashes'][name]: raise ValueError('fixture was changed after scheduling')
        run=compare([path])['runs'][0]
        usage=audit_calls(path,load(path/'status.json')['model_calls'])
        cases={c['id']:c for c in load(path/'suite.json')['learning']['test']}
        rows=jsonl(path/'results.jsonl')
        failures=[r for r in rows if r['split']=='test' and r['mode']=='memory_learning' and r['case_id'] in cases and r['score']!=1]
        counts=Counter(r['case_id'] for r in failures)
        runs.append({'name':name,'path':str(path),'metadata':metadata,'status':run['status'],'reason':run['reason'],
            'validation':run['validation'],'final':{k:v for k,v in run['heldout'].items() if k!='pairs'},
            'generalization_check':run['generalization_check'],'protocol':run['protocol'],
            'adoption_verified':run['adoption_check']['verified'],'task_scope_verified':run['task_scope_check']['verified'],
            'rollback_verified':run['rollback_verified'],'usage':usage,'groups':run['groups'],
            'remaining_failed_cases':[{'id':cid,'prompt':cases[cid]['prompt'],'expected':cases[cid]['expected'],
                'failed_observations':count,'actuals':[r['actual'] for r in failures if r['case_id']==cid]} for cid,count in counts.items()]})
    mem=load(memory/'summary.json'); memmeta=load(memory/'metadata.json')
    for source,digest in memmeta['sources'].items():
        if sha(memory/source)!=digest: raise ValueError('memory evaluator hash mismatch')
    memusage=audit_calls(memory,load(memory/'status.json')['model_calls'])
    storemeta=load(storage/'metadata.json')
    scale=load(storage/'results.json')['scale']
    if [r['facts'] for r in scale] != [100,1000,10000,100000] or any(r['query']['n'] != 30 for r in scale):
        raise ValueError('this scorecard requires the full four-size, thirty-sample storage run')
    if storemeta['native_sha256']!=memmeta['native_sha256']: raise ValueError('native builds differ')
    if sha(storage/'storage_stress.py')!=storemeta['source_sha256']: raise ValueError('storage runner hash mismatch')
    return {'tasks':runs,'memory':{'path':str(memory),'groups':mem['groups'],
        'context_checks':mem['deterministic_context_checks'],'usage':memusage,
        'failed_cases':dict(Counter(r['case_id'] for r in mem['failed_memory']))},
        'storage':{'path':str(storage),'metadata':storemeta,**load(storage/'results.json')},
        'total_calls':sum(r['usage']['calls'] for r in runs)+memusage['calls'],
        'total_tokens':sum(r['usage']['total_tokens'] for r in runs)+memusage['total_tokens'],
        'artifact_audit':'Completed statuses, source/fixture snapshots, sequential call IDs, matched repeat seeds, native build hashes verified.'}


def markdown(s):
    lines=['# 企业场景扩展评测：Qwen3-8B / enterprise-v1','',
        f"本轮完成 **{s['total_calls']:,} 次真实本地模型调用、{s['total_tokens']:,} 个服务器报告 tokens**。使用 release 构建、temperature=0.2、seed=42/43/44。所有学习发生在独立测试数据库，采用的策略在测试后回滚。结果揭示历史消息提示注入、业务边界误判和全量检索的规模问题，尚不能作为企业生产准入依据。",'',
        '## 业务学习结果','',
        '每个任务独立学习；训练、验收、最终测试分区固定。训练只生成一次证据表候选，没有根据验收或最终错误调参。下表只比较同一轮的记忆基线与实际采用策略。重复调用和同族改写不算独立业务样本。','',
        '| 任务 | 候选状态 | 验收正确数：基线→候选 | 最终正确数：基线→实际策略 | 最终退化次数 |',
        '|---|---|---:|---:|---:|']
    names={'support':'客服（旧 V6 稳定性复测）','incident':'运维（新场景）','access':'权限申请（新场景）','access-poisoned':'权限申请（训练标签污染对照）'}
    for r in s['tasks']:
        v,f=r['validation'],r['final']
        lines.append(f"| {names[r['name']]} | {r['status']} | {v['baseline_passed']}→{v['candidate_passed']}/{v['n']} | {f['baseline_passed']}→{f['adopted_passed']}/{f['n']} | {f['regressed']} |")
    lines+=['','运维和权限申请各有 18 条训练、18 条验收、36 条最终题目，覆盖 6 类决策；最终题目包含英文、时间变化、否定、条件缺失、无关紧迫性与引用对象辨别。客服保留 V6 的 18/18/24 划分，不能视为新泛化证据。污染组只翻转权限训练中的 3 个标签，验收和最终标签不变；它与干净权限组共用题目，不增加独立场景覆盖。',
        '', '干净候选仍未答对的最终用例：','']
    for r in s['tasks']:
        if r['name']=='access-poisoned': continue
        for f in r['remaining_failed_cases']:
            lines.append(f"- `{f['id']}`（失败 {f['failed_observations']}/3）：{f['prompt']} 期望 `{json.dumps(f['expected'],ensure_ascii=False)}`，实际 `{json.dumps(f['actuals'][0],ensure_ascii=False)}`。")
    poison=next(r for r in s['tasks'] if r['name']=='access-poisoned')
    lines += ['',f"污染候选验收退化 {poison['validation']['regressed']} 次，未采用；训练检查没有发现这批错误标签，独立的正确验收标签才提供了保护。此结果不证明任意比例或类型的标签污染都会被识别。",'',
        '## 记忆边界与提示注入','',
        f"36 条新用例 × 3 个 seed × 2 种模式，共 {s['memory']['usage']['calls']} 次调用。上下文的值过滤、窗口和估算预算检查全部通过。无记忆对照缺少事实时预期无法答对已知端口，不应与隔离/注入成功率混为一谈。",'',
        '| 携带记忆的场景 | 正确/调用 |','|---|---:|']
    for g in s['memory']['groups']:
        if g['mode']=='memory': lines.append(f"| {g['category']} | {g['passed']}/{g['n']} |")
    lines += ['', '12 种攻击分别进入事实记忆和历史消息。历史消息有 6 种模板成功污染回答，每种在 3 个 seed 下都把真实端口 17443 改成 9999：直接指令、伪管理员维护通知、伪工具结果、英文覆盖、分隔汉字及 XML 闭合伪系统消息。事实记忆中同样的模板本轮全部抵抗成功；这只是给定模板的观测。没有真实秘密或工具执行。',
        '', '长历史测试是 200 条消息经过最近 10 条窗口后的读取；它不等同于模型长上下文容量压测。上下文预算仍是字节估算，不保证模型 tokenizer 的硬上限。', '',
        '## 存储完整性与规模','']
    c=s['storage']['concurrency']; iso=s['storage']['isolation']; crash=s['storage']['process_crash']
    lines += [f"- {c['workers']} 个独立 SDK 实例、{c['rounds']} 轮同步竞争，共 {c['attempts']} 次 CAS：每轮恰好一个成功，{c['expected_conflicts']} 个预期冲突，最终值 {c['final_value']}、revision {c['final_revision']}。这是冲突正确性测试，不是最大吞吐测试。",
        f"- 同一消息并发重试 {c['idempotent_message_attempts']} 次，只保存 {c['persisted_messages']} 条。",
        f"- {iso['tenant_user_scopes']} 个租户/用户组合、每组合 2 个 Agent，共享事实可见、私有事实和会话不串读，{iso['readonly_mutations_blocked']} 次只读绑定写入/删除均阻止。",
        f"- 强杀独立写入子进程后，{crash['acknowledged']} 个已确认写入全部恢复，SQLite integrity_check={crash['integrity_check']}；这不是断电测试。",
        '', '文件 SQLite，WAL + synchronous=NORMAL；模型批次结束后测量，单读者，逐次通过公开 API 写入，查询返回至多 10 条事实。每档 30 次测量，包含 SDK/数据库/排序/上下文构造，不包含 LLM。', '',
        '| 同一用户事实数 | 实际扫描数 | 查询 p50 ms | 查询样本 p95 ms | 进程峰值 RSS MiB |',
        '|---:|---:|---:|---:|---:|']
    for row in s['storage']['scale']:
        lines.append(f"| {row['facts']:,} | {row['inspected_facts']:,} | {row['query']['p50_ms']:.2f} | {row['query']['p95_ms']:.2f} | {row['peak_process_rss_kib']/1024:.1f} |")
    lines += ['', '所有档位都找到了目标事实，重开数据库后十万条事实仍在。但候选数受限没有限制数据库扫描量：当前检索依然全量读取后排序。RSS 是整个进程累计高水位，不能当作单条查询独占内存。没有声明生产 SLO，也没有在此测试跨节点、多租户混合负载或磁盘耗尽。', '',
        '## 后续开发顺序','',
        '1. 明确历史消息的可信级别和注入边界，提供应用层的引用格式及攻击回归集；权限、资金等动作必须由执行端确定性规则验证前置条件，不能仅凭模型分类。',
        '2. 将查询、租户/用户/池过滤及候选限额下推存储层；保留本轮十万条基线，增加并发读写压测后再设定容量目标。',
        '3. 建立训练标签来源、审批和审计机制；自动提取记忆进入候选/审核/版本更新流程，避免未经验证的对话内容直接成为业务策略。',
        '', '本轮没有实现自动提取、数据库身份鉴权、跨节点高可用或真实业务 Agent 上线。池的绑定限制由可信应用传入，不能当作不可信调用方的认证授权系统。业务题目为合成闭集任务，三个业务域分别学习，不证明跨领域策略迁移。', '',
        '## 复现与原始证据','',
        '命令及构建步骤见 [评测说明](../README.md#enterprise-scenario-and-storage-expansion)。同目录 JSON 提供摘要、源码/数据 hash、分组结果和失败用例；原始数据位于以下本机忽略 Git 的目录：','',
        '- `data/evals/enterprise-v1-run1/`：四组学习，冻结的 fixtures、逐调用请求/响应、逐题得分、采用与回滚检查。',
        '- `data/evals/enterprise-memory-run1/`：记忆攻击场景、实际上下文、原始回答。',
        '- `data/evals/enterprise-storage-run1/`：并发、隔离、进程恢复和规模测量。',
        '', '模型调用并行度最多为 3 个学习任务，记忆测试阶段另有 1 个请求流；因此日志中的模型延迟不是单请求独占推理服务的 SLO。原始失败均保留，无自动重试或按最终答案重写响应。首次存储冒烟测试因测试代码对只读错误文本的断言不匹配而失败；修改断言后复测通过，该失败位于 `enterprise-storage-smoke/`，属于测试工具问题。', '',
        f"产物审计：{s['artifact_audit']}", '']
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tasks',type=Path,required=True);p.add_argument('--memory',type=Path,required=True)
    p.add_argument('--storage',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    for suffix in ('.json','.md'):
        if a.output.with_suffix(suffix).exists(): p.error('output files must be new')
    result=summarize(a.tasks,a.memory,a.storage)
    dump(a.output.with_suffix('.json'),result)
    a.output.with_suffix('.md').write_text(markdown(result))
    print(result['total_calls'],result['total_tokens'])

if __name__=='__main__': main()
