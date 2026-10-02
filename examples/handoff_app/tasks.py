"""Explicit task state and trusted, pre-bound local acceptance scripts."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

from project import check_config, digest, inside, relative_path, target, write_json

TASK_PREFIX = 'task.'


def now():
    return datetime.now(timezone.utc).isoformat()


def new_run(config, prefix):
    path = config['runs_dir'] / (prefix + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12])
    path.mkdir(parents=True, exist_ok=False)
    return path


def all_tasks(user):
    result = []
    for record in user.memories():
        key = record.get('fact_key', '')
        if key.startswith(TASK_PREFIX):
            value = record.get('value')
            if (not isinstance(value, dict) or value.get('id') != key[len(TASK_PREFIX):]
                    or value.get('status') not in ('pending', 'done')
                    or type(value.get('revision')) is not int or value['revision'] < 1):
                raise ValueError('Invalid task record: ' + key)
            result.append(value)
    return sorted(result, key=lambda task: task['id'])


def get_task(user, task_id):
    for task in all_tasks(user):
        if task['id'] == task_id:
            return task
    raise ValueError('Task does not exist; use task add: ' + task_id)


def artifact_hashes(config, paths):
    return {name: digest(inside(config['workspace'], name)) for name in paths}


def freshness(task, config):
    try:
        verifier = task['acceptance']['script']
        if digest(inside(config['workspace'], verifier)) != task['acceptance']['sha256']:
            return 'verifier_changed'
        if task['status'] == 'done':
            completion = task['completion']
            if artifact_hashes(config, task['acceptance']['artifacts']) != completion['artifacts']:
                return 'artifacts_changed'
            evidence = inside(config['config_path'].parent, completion['evidence'])
            if digest(evidence) != completion['evidence_sha256']:
                return 'evidence_changed'
            receipt = json.loads(evidence.read_text(encoding='utf-8'))
            if any(digest(evidence.parent / name) != receipt['logs'][name]
                   for name in ('stdout.log', 'stderr.log')):
                return 'evidence_changed'
    except (KeyError, TypeError, ValueError, OSError):
        return 'evidence_unavailable'
    return 'passed' if task['status'] == 'done' else 'not_run'


def add_task(user, config, args):
    if any(task['id'] == args.id for task in all_tasks(user)):
        raise ValueError('Task already exists; inspect task list before creating another: ' + args.id)
    if len(all_tasks(user)) >= 50:
        raise ValueError('This small-project app supports at most 50 tasks per scope')
    script = relative_path(args.verify)
    if not script.endswith('.py'):
        raise ValueError('Use a trusted Python acceptance script ending in .py')
    artifacts = list(dict.fromkeys(relative_path(path) for path in args.artifact))
    if script in artifacts:
        raise ValueError('The frozen verifier cannot also be a delivery artifact')
    if not args.title.strip() or len(args.title) > 300:
        raise ValueError('Task title must contain 1–300 characters')
    task = {'id': args.id, 'title': args.title, 'status': 'pending', 'revision': 1,
            'created_at': now(), 'acceptance': {'script': script,
                'sha256': digest(inside(config['workspace'], script)), 'artifacts': artifacts}}
    user.remember(task, key=TASK_PREFIX + args.id)
    return {'status': 'created', 'task': task, 'writeback': target(config)}


def reopen_task(user, config, args):
    task = get_task(user, args.id)
    if task['revision'] != args.revision:
        raise ValueError('Stale task revision; inspect task list and retry')
    if not args.reason.strip():
        raise ValueError('A reason for reopening is required')
    if digest(inside(config['workspace'], task['acceptance']['script'])) != task['acceptance']['sha256']:
        raise ValueError('Frozen acceptance script changed; restore it before reopening')
    task = dict(task, status='pending', revision=task['revision'] + 1, reopened_at=now(), reopen_reason=args.reason)
    task.pop('completion', None)
    user.remember(task, key=TASK_PREFIX + args.id)
    return {'status': 'reopened', 'task': task, 'writeback': target(config)}


def complete_task(user, config, args):
    task = get_task(user, args.id)
    if task['revision'] != args.revision:
        raise ValueError('Stale task revision; inspect task list and retry')
    fresh = freshness(task, config)
    if task['status'] == 'done':
        if fresh != 'passed':
            raise ValueError('Completed task evidence changed; inspect and explicitly task reopen before verification')
        return {'status': 'already_completed', 'task': task, 'writeback': target(config)}, 0
    if fresh != 'not_run':
        raise ValueError('Acceptance script unavailable or changed; restore the frozen script')
    artifacts_before = artifact_hashes(config, task['acceptance']['artifacts'])
    facts_before = user.memories()
    run = new_run(config, 'verify-' + task['id'])
    evidence = {'schema_version': 1, 'task_id': task['id'], 'task_revision': task['revision'],
                'started_at': now(), 'writeback': target(config), 'acceptance': task['acceptance'],
                'artifacts': artifacts_before, 'status': 'running', 'human_review_status': 'pending'}
    write_json(run / 'started.json', evidence)
    with (run / 'stdout.log').open('xb') as stdout, (run / 'stderr.log').open('xb') as stderr:
        try:
            # Only this explicitly registered, hash-bound script runs. Model text is never executable.
            process = subprocess.run([sys.executable, '-I', str(inside(config['workspace'], task['acceptance']['script']))],
                                     cwd=config['workspace'], stdout=stdout, stderr=stderr, timeout=args.timeout)
            evidence['returncode'] = process.returncode
            evidence['status'] = 'passed' if process.returncode == 0 else 'failed'
        except subprocess.TimeoutExpired:
            evidence.update(status='timed_out', returncode=None)
        except OSError:
            evidence.update(status='launch_failed', returncode=None)
    evidence['finished_at'] = now()
    evidence['logs'] = {name: digest(run / name) for name in ('stdout.log', 'stderr.log')}
    try:
        check_config(config)
        if user.memories() != facts_before:
            raise ValueError('Project memory changed during verification')
        if freshness(task, config) != 'not_run':
            raise ValueError('Acceptance script changed during verification')
        if artifact_hashes(config, task['acceptance']['artifacts']) != artifacts_before:
            raise ValueError('Delivery artifacts changed during verification')
    except (ValueError, OSError) as error:
        evidence.update(status='stale', error=str(error))
    evidence_file = run / 'result.json'
    write_json(evidence_file, evidence)
    if evidence['status'] != 'passed':
        return {'status': 'verification_failed', 'verification': evidence,
                'evidence': str(evidence_file), 'writeback': target(config)}, 2
    completion = {'verified_at': evidence['finished_at'], 'artifacts': artifacts_before,
                  'evidence': evidence_file.relative_to(config['config_path'].parent).as_posix(),
                  'evidence_sha256': digest(evidence_file)}
    updated = dict(task, status='done', revision=task['revision'] + 1, completion=completion)
    user.remember(updated, key=TASK_PREFIX + task['id'])
    return {'status': 'completed', 'task': updated, 'evidence': str(evidence_file),
            'human_review_status': 'pending', 'writeback': target(config)}, 0
