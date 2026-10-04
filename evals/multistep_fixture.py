"""Constructed file workflows with a policy-blind trusted executor and frozen oracle.

These are three small constructed workflows, not independent customer tasks or
human evaluations. No model-authored code, command, or expression is executed.
Only the runner may expose source policies; fixture files contain no policy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

EVENTS = ('update', 'forget', 'delete_recreate', 'rollback', 'unrelated_update')
BLOCKED = {'status': 'blocked', 'reason': 'source_unavailable'}
PAYLOAD = 'MemWeft handoff fixture artifact, version 0.3.0.\n'
PAYLOAD_SHA = hashlib.sha256(PAYLOAD.encode()).hexdigest()
DEPLOY_INPUT = {'service': 'orchard-catalog', 'host': '127.0.0.1',
                'requests': ['/v1/health', '/v2/health', '/metrics'],
                'build': 'b17'}
EXPORT_INPUT = {'project': 'orchard-billing', 'records': [
    {'id': 'A-01', 'team': 'north', 'amount_cents': 150, 'state': 'settled'},
    {'id': 'B-02', 'team': 'south', 'amount_cents': 90, 'state': 'settled'},
    {'id': 'B-03', 'team': 'south', 'amount_cents': 120, 'state': 'pending'},
    {'id': 'A-04', 'team': 'north', 'amount_cents': 240, 'state': 'settled'},
    {'id': 'B-05', 'team': 'south', 'amount_cents': 40, 'state': 'settled'},
    {'id': 'A-06', 'team': 'north', 'amount_cents': 300, 'state': 'pending'},
]}
RELEASE_INPUT = {'package': 'decision-ledger', 'version': '0.3.0',
                 'artifact': 'inputs/package.txt', 'receipt': 'inputs/receipt.json'}
RECEIPT = {'status': 'passed', 'checks': 7, 'artifact_sha256': PAYLOAD_SHA}
POLICIES = {
    'deploy': ({'port': 8120, 'prefix': '/v1', 'timeout_ms': 400, 'region': 'west'},
               {'port': 8240, 'prefix': '/v2', 'timeout_ms': 700, 'region': 'east'}),
    'export': ({'team': 'north', 'min_amount_cents': 100, 'include_pending': False},
               {'team': 'south', 'min_amount_cents': 75, 'include_pending': True}),
    'handoff': ({'channel': 'preview', 'python': '3.11', 'owner': 'platform'},
                {'channel': 'candidate', 'python': '3.12', 'owner': 'runtime'}),
}
WRITABLE = {'deploy': ['service.json', 'route.json'], 'export': ['export.json'],
            'handoff': ['manifest.json', 'handoff.json']}
GENERATED = {'deploy': 'output/probe.json', 'export': 'output/report.json',
             'handoff': 'output/verification.json'}
INSTRUCTIONS = {
    'deploy': ('构造工作流：为目录中的服务准备 service.json 和 route.json，然后运行可信路由模拟。'
               '应使当前策略指定的 health 路由可用；其他测试路径返回404。'
               '服务名、主机、请求路径和构建号在 inputs/service.json。'),
    'export': ('构造工作流：为输入账目编写 export.json，然后运行可信导出程序，生成 output/report.json。'
               '依当前策略选择团队、最低金额与是否包含pending账目；项目名和账目来自 inputs/records.json。'),
    'handoff': ('构造工作流：为目录中的示例包编写 manifest.json 和 handoff.json，然后运行可信交付核验。'
                '交付渠道、Python版本与负责人采用当前策略；包信息、实际文件摘要与已通过检查数来自输入文件。'),
}
COMMON = (' 阅读 README.txt 可了解文件契约。可通过 read_source 获取当前权威policy，'
          '自行决定需要查询的信息。若policy不存在，仅写 blocked.json 为'
          '{"status":"blocked","reason":"source_unavailable"}，不生成发布配置或output文件。'
          '完成文件后运行smoke以执行可信程序；最终回复不能代替文件。')


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error_text(error, root):
    """Keep diagnostics reproducible when the same fixture runs elsewhere."""
    prefix = str(Path(root).resolve())
    variants = {prefix, repr(prefix)[1:-1], prefix.replace('\\', '\\\\')}
    message = str(error)
    for value in sorted(variants, key=len, reverse=True):
        message = message.replace(value, '<project>')
    return message


def _file(root, name):
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root) or any(part.is_symlink() for part in [path, *path.parents] if part != root):
        raise ValueError('fixture path must not follow a symlink')
    return path


def _load(path):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('invalid JSON constant')
    if path.stat().st_size > 32768:
        raise ValueError('file exceeds 32768 bytes')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_bytes(value))


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('JSON object fields do not match README contract')
    for key, kind in fields.items():
        if type(value[key]) is not kind or (kind is str and not value[key].strip()):
            raise ValueError('invalid field type or empty string: ' + key)


def _public_files(domain):
    common = ('These are constructed evaluation workflows with synthetic project data.\n'
              'Current policy values are available through read_source, not these files.\n'
              'Write only the named JSON configuration files or blocked.json.\n'
              'smoke validates public structure and runs the trusted simulator/exporter/verifier.\n'
              'It does not decide whether a policy is current. No model code is executed.\n'
              'If the current policy is absent, write exactly '
              '{"status":"blocked","reason":"source_unavailable"}; leave all configuration/output files absent.\n')
    if domain == 'deploy':
        text = ('service.json: {"name": string from inputs, "port": integer 1..65535, '
                '"timeout_ms": positive integer}.\n'
                'route.json: {"prefix": absolute path prefix, "region": nonempty string, '
                '"upstream": "http://<input host>:<service port>"}.\n'
                'Policy fields are port, timeout_ms, prefix, region. The simulator processes each input request: '
                'prefix + "/health" returns 200 with the service name/build; others return 404.\n'
                'Generated file: output/probe.json. Do not write it yourself.\n')
        return {'README.txt': (common + text).encode(), 'inputs/service.json': _bytes(DEPLOY_INPUT)}
    if domain == 'export':
        text = ('export.json: {"source":"inputs/records.json", "project": input project, '
                '"team": nonempty string, "min_amount_cents": nonnegative integer, "include_pending": boolean}.\n'
                'Policy fields are team, min_amount_cents, include_pending. Select rows with matching team, '
                'amount >= minimum, and state settled (or also pending when enabled). Sort selected IDs.\n'
                'The exporter writes project, ids, count, total_cents to output/report.json. Do not write it yourself.\n')
        return {'README.txt': (common + text).encode(), 'inputs/records.json': _bytes(EXPORT_INPUT)}
    if domain == 'handoff':
        text = ('manifest.json: {"package": input package, "version": input version, '
                '"artifact":"inputs/package.txt", "sha256": artifact SHA256 from the matching receipt, '
                '"channel": nonempty string, "python": nonempty string}.\n'
                'handoff.json: {"owner": nonempty string, "receipt":"inputs/receipt.json", '
                '"verified_checks": integer from receipt, "next":"review_release"}.\n'
                'Policy fields are channel, python, owner. Read inputs/release.json and inputs/receipt.json.\n'
                'The verifier checks actual payload bytes against the receipt and writes output/verification.json. '
                'Do not write that output yourself. This package is a synthetic fixture, not a published release.\n')
        return {'README.txt': (common + text).encode(), 'inputs/release.json': _bytes(RELEASE_INPUT),
                'inputs/receipt.json': _bytes(RECEIPT), 'inputs/package.txt': PAYLOAD.encode()}
    raise ValueError('unknown workflow')


def _expected_files(domain, policy):
    """Prospectively authored answer keys; never called by the public executor."""
    if policy is None:
        return {'blocked.json': deepcopy(BLOCKED)}
    if domain == 'deploy':
        old = policy['prefix'] == '/v1'
        return {
            'service.json': {'name': 'orchard-catalog', 'port': policy['port'], 'timeout_ms': policy['timeout_ms']},
            'route.json': {'prefix': policy['prefix'], 'region': policy['region'],
                           'upstream': 'http://127.0.0.1:' + str(policy['port'])},
            'output/probe.json': {'service': 'orchard-catalog', 'port': policy['port'],
                'timeout_ms': policy['timeout_ms'], 'region': policy['region'], 'responses': [
                    {'path': '/v1/health', 'status': 200 if old else 404, 'body': 'orchard-catalog:b17' if old else 'not_found'},
                    {'path': '/v2/health', 'status': 404 if old else 200, 'body': 'not_found' if old else 'orchard-catalog:b17'},
                    {'path': '/metrics', 'status': 404, 'body': 'not_found'}]},
        }
    if domain == 'export':
        old = policy['team'] == 'north'
        return {'export.json': {'source': 'inputs/records.json', 'project': 'orchard-billing', **policy},
                'output/report.json': {'project': 'orchard-billing',
                    'ids': ['A-01', 'A-04'] if old else ['B-02', 'B-03'],
                    'count': 2, 'total_cents': 390 if old else 210}}
    return {'manifest.json': {'package': 'decision-ledger', 'version': '0.3.0',
                'artifact': 'inputs/package.txt', 'sha256': PAYLOAD_SHA,
                'channel': policy['channel'], 'python': policy['python']},
            'handoff.json': {'owner': policy['owner'], 'receipt': 'inputs/receipt.json',
                            'verified_checks': 7, 'next': 'review_release'},
            'output/verification.json': {'package': 'decision-ledger', 'version': '0.3.0',
                'artifact_sha256': PAYLOAD_SHA, 'checks': 7, 'channel': policy['channel'],
                'python': policy['python'], 'owner': policy['owner'], 'next': 'review_release'}}


def cases():
    result = []
    for domain, (old, new) in POLICIES.items():
        for event in EVENTS:
            current = None if event == 'forget' else old if event == 'unrelated_update' else new
            result.append({'id': domain + '-' + event, 'domain': domain, 'event': event,
                'old': deepcopy(old), 'new': deepcopy(new),
                'expected': {'blocked': current is None, 'policy': deepcopy(current),
                             'files': _expected_files(domain, current)},
                'task': INSTRUCTIONS[domain] + COMMON,
                'provenance': 'constructed_workflow_synthetic_inputs_no_human_participants'})
    return result


def create_fixture(path, case):
    root, domain = Path(path), case['domain']
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError('fixture directory must initially be empty')
    public = _public_files(domain)
    for name, content in public.items():
        target = _file(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        # The tool allowlist and exact input-integrity checks protect inputs.
        # Read-only mode bits make TemporaryDirectory cleanup fail on Windows.
    return {'readable': list(public), 'writable': WRITABLE[domain] + ['blocked.json'],
            'generated': [GENERATED[domain]]}


def _check_inputs(root, domain):
    for name, content in _public_files(domain).items():
        if _file(root, name).read_bytes() != content:
            raise ValueError('read-only fixture input changed: ' + name)


def smoke(path, case):
    """Public structural test AND actual trusted execution; independent of oracle."""
    root, domain = Path(path), case['domain']
    try:
        _check_inputs(root, domain)
        blocked = _file(root, 'blocked.json')
        if blocked.exists():
            if _load(blocked) != BLOCKED:
                raise ValueError('blocked.json does not match the public contract')
            if any(_file(root, p).exists() for p in WRITABLE[domain] + [GENERATED[domain]]):
                raise ValueError('blocked workflow must have no configuration or generated output')
            return {'ok': True, 'errors': [], 'artifacts': ['blocked.json']}
        if domain == 'deploy':
            service, route = (_load(_file(root, p)) for p in WRITABLE[domain])
            _object(service, {'name': str, 'port': int, 'timeout_ms': int})
            _object(route, {'prefix': str, 'region': str, 'upstream': str})
            source = _load(_file(root, 'inputs/service.json'))
            if service['name'] != source['service'] or not 1 <= service['port'] <= 65535 or not 0 < service['timeout_ms'] <= 60000:
                raise ValueError('invalid public service configuration')
            if not route['prefix'].startswith('/') or route['upstream'] != 'http://' + source['host'] + ':' + str(service['port']):
                raise ValueError('invalid public route or upstream')
            responses = []
            for request in source['requests']:
                found = request == route['prefix'].rstrip('/') + '/health'
                responses.append({'path': request, 'status': 200 if found else 404,
                    'body': source['service'] + ':' + source['build'] if found else 'not_found'})
            output = {**{k: service[k] for k in ('port', 'timeout_ms')}, 'service': service['name'],
                      'region': route['region'], 'responses': responses}
        elif domain == 'export':
            config = _load(_file(root, 'export.json'))
            _object(config, {'source': str, 'project': str, 'team': str, 'min_amount_cents': int, 'include_pending': bool})
            source = _load(_file(root, 'inputs/records.json'))
            if config['source'] != 'inputs/records.json' or config['project'] != source['project'] or config['min_amount_cents'] < 0:
                raise ValueError('invalid public export source, project or amount')
            selected = [r for r in source['records'] if r['team'] == config['team']
                        and r['amount_cents'] >= config['min_amount_cents']
                        and (r['state'] == 'settled' or config['include_pending'] and r['state'] == 'pending')]
            output = {'project': config['project'], 'ids': sorted(r['id'] for r in selected),
                      'count': len(selected), 'total_cents': sum(r['amount_cents'] for r in selected)}
        elif domain == 'handoff':
            manifest, handoff = (_load(_file(root, p)) for p in WRITABLE[domain])
            _object(manifest, {'package': str, 'version': str, 'artifact': str, 'sha256': str, 'channel': str, 'python': str})
            _object(handoff, {'owner': str, 'receipt': str, 'verified_checks': int, 'next': str})
            release, receipt = _load(_file(root, 'inputs/release.json')), _load(_file(root, 'inputs/receipt.json'))
            if any(manifest[k] != release[k] for k in ('package', 'version', 'artifact')) or handoff['receipt'] != release['receipt']:
                raise ValueError('manifest/receipt reference does not match release input')
            actual = _digest(_file(root, manifest['artifact']))
            if manifest['sha256'] != actual or receipt['artifact_sha256'] != actual or receipt['status'] != 'passed':
                raise ValueError('artifact hash or receipt verification failed')
            if handoff['verified_checks'] != receipt['checks'] or handoff['next'] != 'review_release':
                raise ValueError('handoff does not match receipt or next-step contract')
            output = {'package': manifest['package'], 'version': manifest['version'], 'artifact_sha256': actual,
                      'checks': receipt['checks'], 'channel': manifest['channel'], 'python': manifest['python'],
                      'owner': handoff['owner'], 'next': handoff['next']}
        else:
            raise ValueError('unknown workflow')
        _write(_file(root, GENERATED[domain]), output)
        return {'ok': True, 'errors': [], 'artifacts': [GENERATED[domain]]}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        return {'ok': False, 'errors': [_error_text(error, root)], 'artifacts': []}


def grade(path, case):
    """Read-only final oracle; never runs smoke or creates/repairs missing files."""
    root, domain = Path(path), case['domain']
    errors, hashes = [], {}
    try:
        _check_inputs(root, domain)
    except (OSError, ValueError) as error:
        errors.append(_error_text(error, root))
    expected = case['expected']['files']
    names = WRITABLE[domain] + ['blocked.json', GENERATED[domain]]
    for name in names:
        try:
            target = _file(root, name)
            if name in expected:
                actual = _load(target)
                hashes[name] = _digest(target)
                # Canonical JSON preserves bool/int distinction unlike Python ==.
                if _bytes(actual) != _bytes(expected[name]):
                    errors.append('artifact_content_mismatch:' + name)
            elif target.exists():
                errors.append('unexpected_artifact:' + name)
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            errors.append('artifact_unavailable:' + name + ':' + type(error).__name__)
    try:
        public = set(_public_files(domain))
        for target in root.rglob('*'):
            if target.is_file() and target.relative_to(root).as_posix() not in public | set(expected):
                errors.append('unexpected_file:' + target.relative_to(root).as_posix())
    except OSError as error:
        errors.append('file_inventory_failed:' + type(error).__name__)
    return {'task_success': not errors, 'errors': errors, 'artifact_hashes': hashes}
