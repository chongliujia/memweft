"""Portable project configuration and cooperating-CLI write exclusion."""
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tempfile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or Path(value).is_absolute() or '..' in Path(value).parts or ':' in value):
        raise ValueError('Use a relative path without .., backslashes or drive prefixes')
    return value


def inside(root, value):
    path = (root / relative_path(value)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Configured path leaves the project directory')
    return path


def load_config(path, identifier):
    path = path.resolve()
    config = json.loads(path.read_text(encoding='utf-8'))
    fields = {'schema_version', 'project', 'user', 'database', 'workspace', 'runs_dir', 'required_facts'}
    if not isinstance(config, dict) or set(config) != fields or config['schema_version'] != 1:
        raise ValueError('Unsupported project configuration; create one with init')
    for key in ('project', 'user'):
        if not isinstance(config[key], str):
            raise ValueError('Project and user must be identifiers')
        identifier(config[key])
    required = config['required_facts']
    if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
        raise ValueError('required_facts must be an array of fact keys')
    for key in required:
        identifier(key)
    if len(set(required)) != len(required):
        raise ValueError('required_facts must not contain duplicates')
    result = dict(config, config_path=path, config_sha256=digest(path))
    for key in ('database', 'workspace', 'runs_dir'):
        result[key] = inside(path.parent, config[key])
    if result['database'].is_dir():
        raise ValueError('Configured database must be a file path')
    if result['runs_dir'].exists() and not result['runs_dir'].is_dir():
        raise ValueError('Configured runs_dir must be a directory')
    if not result['workspace'].is_dir():
        raise ValueError('Configured workspace does not exist')
    return result


def check_config(config):
    if digest(config['config_path']) != config['config_sha256']:
        raise ValueError('Project configuration changed during this operation; retry from current state')


def target(config):
    return {'database': str(config['database']), 'project': config['project'],
            'user': config['user'], 'tenant': 'handoff:' + config['project'],
            'agent': 'project-handoff', 'config_sha256': config['config_sha256']}


@contextmanager
def project_lock(config):
    db = config['database']
    db.parent.mkdir(parents=True, exist_ok=True)
    path = db.with_name(db.name + '.handoff-lock')
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('Project is locked by another CLI operation. If a process crashed, confirm it stopped before removing ' + str(path)) from None
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'pid': os.getpid()}, stream)
        check_config(config)
        yield
    finally:
        path.unlink()


def doctor(config=None, model='none'):
    checks = []
    def add(name, ok, detail):
        checks.append({'name': name, 'ok': ok, 'detail': detail})
    add('python', sys.version_info >= (3, 10),
        {'version': platform.python_version(), 'executable': sys.executable,
         'platform': sys.platform, 'architecture': platform.machine(), 'required': '>=3.10'})
    try:
        from memweft import Memory
        with tempfile.TemporaryDirectory(prefix='memweft-doctor-') as directory:
            with Memory(str(Path(directory) / 'probe.db')) as memory:
                user = memory.user('doctor', tenant_id='handoff:doctor', agent_id='project-handoff')
                user.remember('ok', key='probe')
                ok = any(record.get('value') == 'ok' for record in user.memories())
        distribution = importlib.metadata.distribution('memweft')
        add('sdk', ok, {'version': distribution.version, 'wheel_metadata': distribution.read_text('WHEEL'),
                        'probe': 'temporary database write/read',
                        'hint': 'Use the wheel matching this interpreter, OS and architecture'})
    except (ImportError, OSError, RuntimeError, ValueError, importlib.metadata.PackageNotFoundError) as error:
        add('sdk', False, {'error_type': type(error).__name__,
                           'hint': 'Install a MemWeft wheel matching this interpreter, OS and architecture'})
    if config:
        parent = config['database'].parent
        while not parent.exists():
            parent = parent.parent
        add('database_parent_writable', parent.is_dir() and os.access(parent, os.W_OK), str(parent))
        lock = config['database'].with_name(config['database'].name + '.handoff-lock')
        add('project_unlocked', not lock.exists(), str(lock))
    present = bool(os.environ.get('MOONSHOT_API_KEY', '').strip())
    add('model_credentials', model != 'kimi' or present,
        {'model': model, 'environment_key_present': present,
         'hint': 'For Kimi set MOONSHOT_API_KEY, or use resume --prompt-key at runtime'})
    return {'status': 'ok' if all(check['ok'] for check in checks) else 'needs_setup',
            'checks': checks, 'writeback': target(config) if config else None,
            'model_calls': 0}
