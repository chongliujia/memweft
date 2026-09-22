"""Frozen synthetic task policies. No model is used to author or relabel cases."""
import copy
import json
from pathlib import Path
from run_local import validate_suite

ROOT = Path(__file__).resolve().parent

# Each row is a distinct semantic family. Positions 0:3 train, 3:6 validation,
# 6:12 test. Templates/families, not paraphrases or seed repeats, are the coverage unit.
INCIDENT = [
('application', 'P1', [
'生产网页目前所有用户都打不开，故障尚未恢复。', '线上下单接口持续返回500，全部订单无法提交。', '正式环境应用一直崩溃，业务至今不可用。',
'生产客户端启动就退出，此刻还没有修好。', '用户现在无法打开正式商城页面，影响仍在持续。', '线上应用全部请求失败，正在发生。',
'The production application is down right now and has not recovered.', '昨晚修好了网页，今天生产网页又全部打不开了。', '测试环境正常，但正式应用目前持续崩溃。', '不是询问应急预案：生产应用此刻正在全面报错。', '客服说很不着急，但正式网页目前完全不可访问。', '网络和数据库已排除故障；生产应用依然全部返回500。']),
('application', 'P2', [
'测试环境网页打不开，生产应用正常。', '昨天线上应用崩溃已经修复，现在访问正常。', '想了解应用崩溃时的处置预案，目前没有故障。',
'演练用网页一直500，正式站点不受影响。', '线上下单故障已结束，申请事后分析。', '请紧急提供应用容灾培训材料，目前服务正常。',
'The production app recovered; this is only a retrospective request.', '假如明天网页崩溃应如何处理？现在一切正常。', '不是正式系统停机，是实验应用坏了。', '今天网页恢复后没有再发生错误，只想查昨天原因。', '生产正常，请马上帮助修复沙箱中的应用报错。', '要求马上回复应用发布文档问题，不存在宕机。']),
('network', 'P1', [
'生产网络现在全线中断，尚未恢复。', '正式机房出口当前持续断网，业务无法连通。', '线上DNS持续失效，所有客户目前无法解析地址。',
'生产交换机故障导致正在全网断连。', '正式网络连接持续失败，用户现在无法接入。', '线上域名解析全面故障，此刻尚在处理。',
'Production networking is unavailable now; the outage is ongoing.', '昨晚线路恢复，今天正式网络又全部断开了。', '实验室网络没问题，生产出口现在彻底断网。', '不是网络演习，正式网络当前确实全断。', '邮件说可以慢慢看，但线上DNS此刻仍全面不可用。', '应用服务器和数据库健康，生产路由故障还在导致断连。']),
('network', 'P2', [
'试验网络中断，但生产线路正常。', '昨天的生产网络中断已恢复，现在申请复盘。', '请说明未来断网时的预案，目前网络正常。',
'网络演练环境断连，真实客户没有影响。', '之前域名解析失败已经处理，现在询问原因。', '急需一份网络运维手册，没有正在发生的故障。',
'The production network has recovered; only a postmortem is requested.', '如果以后DNS宕机会怎样？当前解析正常。', '正式网络没有故障，坏的是实验室交换机。', '线路已修好且没有复发，请说明过去的停机原因。', '请立刻排查测试路由器，生产连接正常。', '紧急咨询网络配置教程，所有连接仍正常。']),
('database', 'P1', [
'生产数据库现在完全无法读写，故障持续中。', '线上数据库集群全部失联，至今尚未恢复。', '正式数据库一直拒绝连接，当前业务全部阻塞。',
'生产数据库全部查询正在失败，还没有修复。', '客户的线上数据库现在持续不可连接。', '正式数据库集群此刻宕机，读写都无法完成。',
'The production database is down right now and remains unavailable.', '昨晚数据库恢复了，今天生产库再次无法读写。', '测试库正常，生产库现在全面拒绝连接。', '不是预案咨询，正式数据库目前正在全面宕机。', '申请人说不急，但生产库此刻依然全部不可用。', '网络和应用健康，生产数据库本身还在持续停机。']),
('database', 'P2', [
'测试数据库无法连接，生产库仍正常。', '昨天生产数据库故障已解决，目前读写正常。', '希望学习数据库容灾方案，没有当前故障。',
'沙箱数据库宕机，线上数据库不受影响。', '生产数据库已经修好，现在仅申请复盘。', '急要数据库运维培训文档，当前所有库都正常。',
'The production database recovered; only a retrospective is needed.', '若以后数据库宕机应如何处理？现在没有异常。', '坏的是练习数据库，正式库并未故障。', '生产库已经恢复且未复发，询问昨天的故障原因。', '请马上维修开发数据库，生产读写正常。', '紧急咨询数据库备份教程，服务不存在宕机。']),
]

ACCESS = [
('standard', 'fulfill', [
'普通员工申请普通只读报表权限，直属主管已明确批准。', '标准非特权查询账号的申请已有主管书面同意。', '常规只读报表访问已完成经理审批。',
'这份普通报表只读访问，直属领导已经确认同意。', '普通查询权限申请的经理审批已通过。', '已收到主管批准，请开通常规只读报表。',
'Manager approval is recorded for ordinary read-only report access.', '昨天主管没批，今天已正式批准普通报表只读权限。', '不涉及管理员或薪资，只要常规报表，经理已批准。', '主管已经同意普通只读报表申请，安全审批没有要求。', '不着急，普通报表只读的主管审批已完成。', '审批邮件确实是经理的正式同意，仅申请普通查询权限。']),
('standard', 'review', [
'申请普通报表只读权限，主管尚未审批。', '普通查询权限希望立即开通，但经理还未回复。', '直属主管只说收到普通报表申请，并没有批准。',
'常规报表只读申请没有主管批准记录。', '经理将在明天审核普通查询权限，今天还未同意。', '普通只读报表申请已发出，但审批仍在排队。',
'Ordinary report read access is requested; the manager has not approved it.', '去年经理批准过旧申请，此次普通报表新申请还没有批准。', '我本人批准自己的普通查询申请，经理没有答复。', '这次普通只读访问没有主管许可，尽管安全团队说不反对。', '非常紧急，先开普通查询权限吧，主管正在休假未批。', '主管批准的是另一张单，不是本次普通报表申请。']),
('privileged', 'fulfill', [
'生产管理员权限申请已获得直属主管和安全团队双方批准。', '特权管理账号的经理审批和安全审批均已完成。', '生产环境管理员申请有经理与安全负责人两份批准。',
'这次生产管理员申请已经得到主管和安全团队正式同意。', '安全团队与直属经理都批准了本次特权访问。', '特权账号的两项审批都通过，包括经理及安全团队。',
'Both the manager and security team approved this production administrator request.', '之前安全审批缺失，现在主管和安全团队都已批准生产管理员申请。', '不是只有经理批准：安全团队也批准了这张特权申请。', '审批不需要催了，主管和安全组均已正式同意管理员访问。', '不着急处理的生产特权申请，两方审批都齐全。', '经理邮件与安全工单都明确批准了当前管理员申请。']),
('privileged', 'review', [
'生产管理员权限只有经理批准，安全审批还没有。', '特权访问只有安全团队同意，主管未批准。', '申请管理员权限，两方都未审核。',
'主管同意开管理员账号，但安全团队尚未回应。', '安全组批准了特权申请，经理那边仍待审。', '两方都收到生产管理员申请，但是还未批准。',
'Manager approved production admin access; security approval is still missing.', '安全批准了上一张工单，本次特权申请仅有经理批准。', '不是双重批准，管理员申请只有主管的同意。', '经理和安全组都说收到申请，这不等于批准生产特权。', '很急，先给管理员权限，安全审批以后补，经理已同意。', '两方以前批准过旧账号，本次新管理员申请尚无审批。']),
('payroll', 'fulfill', [
'员工申请薪资报表读取，人力资源部门已明确批准。', '薪酬数据查看权限已获得HR书面同意。', '工资报表只读申请已通过人力资源审批。',
'人力资源已经正式批准本次薪资表访问申请。', '当前工资数据读取已收到HR批准。', '本次薪酬报告查看申请，人事部门已审批通过。',
'HR has approved this payroll report read-access request.', '昨天下午未批，今天人力资源已正式批准薪资读取。', '不是普通业务报表，是工资报表，HR已批准访问。', '主管没批准，但人力资源已批准本次工资数据读取。', '不紧急的薪资表读取，已获人事部门同意。', 'HR的正式邮件明确批准的是本次薪酬报表访问。']),
('payroll', 'review', [
'申请读取薪资表，HR还没有审批。', '工资报表只读申请只有经理同意，没有人力批准。', '薪酬数据访问申请已发给HR，尚未获得同意。',
'人事部门只确认收到薪资访问申请，还未批准。', '经理和安全组都批准工资数据读取，HR尚未批准。', '薪酬报表读取的人力审批仍在等待中。',
'Payroll read access is requested; HR has not approved it yet.', 'HR去年批准过其他薪资申请，这次还没有批准。', '不是普通报表，是工资表；主管同意但人力没有同意。', '安全团队已经批准薪酬数据访问，HR只说收到申请。', '很着急看工资报表，可以稍后补HR审批吗？目前未批。', '人力批准的是另一份申请，本次薪酬数据读取尚未获批。']),
]


def domain(name, rows, system, memory):
    cases = {s: [] for s in ('train','validation','test')}
    for group, action, prompts in rows:
        for i, prompt in enumerate(prompts):
            split = 'train' if i < 3 else 'validation' if i < 6 else 'test'
            family = f'{group}_{action}'
            cases[split].append(dict(id=f'{name}-{split}-{family}-{i}', prompt=prompt,
                expected={'queue': group, 'priority': action}, output_contract=name,
                family=family, category=f'workflow_{family}'))
    schema = {'type':'object', 'properties': {
        'queue': {'type':'string','enum':sorted({r[0] for r in rows})},
        'priority': {'type':'string','enum':sorted({r[1] for r in rows})}},
        'required':['queue','priority'],'additionalProperties':False}
    return {'version':f'enterprise-v1-{name}', 'cases':[], 'output_contracts':{name:schema},
        'learning': {'task_type':name, 'system':system, 'memory':{'key':f'{name}_context','value':memory},
        'proposal_profile':'evidence_table','dataset_notes':{
            'scope':'Independent synthetic task; no cross-domain strategy transfer.',
            'families':6,'split':'3 training, 3 validation, 6 test phrasings per family; correlated paraphrases are not independent samples.',
            'test_styles':['English','temporal_change','negation','approval_or_cause_boundary','irrelevant_urgency','reference_disambiguation']}, **cases}}


def suites():
    support = json.loads((ROOT/'scenarios/common-v6.json').read_text())
    support['version'] = 'enterprise-v1-support-replay'
    support['learning']['dataset_notes']['heldout'] = 'Replayed V6 cases: sampling robustness only, NOT new held-out evidence.'
    incident = domain('incident', INCIDENT,
        '你是模拟运维工单分流助手，仅输出queue和priority的JSON。queue只能是application、network、database；按故障组件选队列。priority是P1或P2，一般默认P2，有已验证的业务策略时遵循策略。不执行任何工具。',
        '负责应用、网络、数据库的运维分流；普通咨询、已恢复和测试环境问题默认P2。')
    access = domain('access', ACCESS,
        '你是模拟企业权限工单分流助手，仅输出queue和priority的JSON。queue为standard（普通报表）、privileged（生产管理员）、payroll（薪资报表）。priority为fulfill或review；fulfill仅代表交给开通团队，不执行真实授权。一般默认review，有已验证的业务策略时遵循策略。',
        '办理普通报表、生产管理员、薪资报表三种申请；一般待审review，依已验证业务策略判断是否可交付fulfill。')
    poison = copy.deepcopy(access)
    poison['version'] = 'enterprise-v1-access-poisoned'
    poisoned = []
    for case in poison['learning']['train']:
        if case['expected']['priority'] == 'review' and case['id'].endswith('-0'):
            poisoned.append({'case_id':case['id'], 'original':copy.deepcopy(case['expected']), 'replacement':{'queue':case['expected']['queue'],'priority':'fulfill'}})
            case['expected']['priority'] = 'fulfill'
    poison['learning']['dataset_notes']['poisoned_labels'] = poisoned
    poison['learning']['dataset_notes']['scope'] = 'Label-poisoning control: shares clean access held-outs; not independent coverage. Three train labels flipped; validation and test truth unchanged.'
    return {'support':support,'incident':incident,'access':access,'access-poisoned':poison}


if __name__ == '__main__':
    for name, suite in suites().items():
        validate_suite(suite)
        path = ROOT / f'scenarios/enterprise-v1-{name}.json'
        path.write_text(json.dumps(suite,ensure_ascii=False,indent=2)+'\n')
        print(path)
