"""Public file tools wired to the application completion gate, separate from v2.

No expected policy, hidden oracle, or historical success label is passed to this
adapter. The source snapshot is fixed for the life of this serial fixture task.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from verified_completion import TrustedTool, VerifiedSession, file_snapshot
from multistep_fixture import _file, smoke


def make_session(project, domain, files, current_sources, *, max_rounds=4):
    project, files, sources = Path(project), deepcopy(files), deepcopy(current_sources)
    if domain not in ('deploy', 'export', 'handoff'):
        raise ValueError('Unknown public workflow')

    def fields(action, expected):
        if set(action) != {'tool', *expected}:
            raise ValueError('Invalid tool fields')

    def read(action):
        fields(action, {'paths'})
        names = action['paths']
        allowed = files['readable'] + files['writable'] + files['generated']
        if (not isinstance(names, list) or not 1 <= len(names) <= 6
                or any(not isinstance(name, str) or name not in allowed for name in names)):
            raise ValueError('Read path not permitted or too many paths')
        return {name: _file(project, name).read_text(encoding='utf-8')
                if _file(project, name).exists() else None for name in names}

    def read_source(action):
        fields(action, {'key'})
        if not isinstance(action['key'], str):
            raise ValueError('Source key must be text')
        return deepcopy(sources.get(action['key']))

    def write(action):
        fields(action, {'path', 'value'})
        name, value = action['path'], action['value']
        if not isinstance(name, str) or name not in files['writable'] or not isinstance(value, dict):
            raise ValueError('Write path not permitted or value is not an object')
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
            raise ValueError('Write exceeds byte cap')
        _file(project, name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                                   allow_nan=False) + '\n', encoding='utf-8')
        return {'written': name}

    def remove(action):
        fields(action, {'path'})
        name = action['path']
        if not isinstance(name, str) or name not in files['writable'] + files['generated']:
            raise ValueError('Remove path not permitted')
        _file(project, name).unlink(missing_ok=True)
        return {'removed': name}

    def run(action):
        fields(action, set())
        # smoke receives only the public workflow identity, never case.expected.
        return smoke(project, {'domain': domain})

    return VerifiedSession({
        'read': TrustedTool(read, 'read'),
        'read_source': TrustedTool(read_source, 'read'),
        'write': TrustedTool(write, 'mutate'),
        'remove': TrustedTool(remove, 'mutate'),
        'run': TrustedTool(run, 'verify'),
    }, snapshot=lambda: file_snapshot(project), max_rounds=max_rounds)


def feedback(execution):
    """Only public tool outcomes and completion state belong in the next prompt."""
    return {'tool_results': execution['results'], 'completion': execution['completion'],
            'done_accepted': execution['done_accepted'], 'status': execution['status'],
            'protocol_error': execution['protocol_error'],
            'rounds_remaining': execution['rounds_remaining']}
