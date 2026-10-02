"""Configured project continuation; current memory precedes every model proposal."""
import json
from importlib.metadata import version
from pathlib import Path

from project import check_config, digest, doctor, load_config, project_lock, target, write_json
from tasks import TASK_PREFIX, add_task, all_tasks, complete_task, freshness, new_run, reopen_task


def run(args, parser):
    from handoff import PREFIX, facts_from, identifier, now, render
    config_path = args.config or Path('handoff.json')
    if args.command == 'init':
        if not args.project:
            parser.error('init requires --project')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        value = {'schema_version': 1, 'project': args.project, 'user': args.user or 'maintainer',
                 'database': args.db.as_posix() if args.db else 'data/handoff.db', 'workspace': args.workspace,
                 'runs_dir': args.runs_dir, 'required_facts': list(dict.fromkeys(args.require))}
        # Validate before creating the config; failed validation must not leave a broken project.
        from project import inside
        for name in ('database', 'workspace', 'runs_dir'):
            inside(config_path.resolve().parent, value[name])
        if inside(config_path.resolve().parent, value['database']).is_dir():
            raise ValueError('Configured database must be a file path')
        runs_path = inside(config_path.resolve().parent, value['runs_dir'])
        if runs_path.exists() and not runs_path.is_dir():
            raise ValueError('Configured runs_dir must be a directory')
        if not inside(config_path.resolve().parent, value['workspace']).is_dir():
            raise ValueError('Workspace does not exist')
        write_json(config_path, value)
        config = load_config(config_path, identifier)
        return {'status': 'initialized', 'config': str(config_path.resolve()), 'writeback': target(config)}, 0
    config = load_config(config_path, identifier) if config_path.exists() else None
    if args.config and config is None:
        raise ValueError('Configuration does not exist; run init first')
    if config and any(value is not None for value in (args.db, args.project, args.user)):
        raise ValueError('Configured projects bind database/project/user; edit the config explicitly instead of overriding them')
    if args.command == 'doctor':
        result = doctor(config, args.model)
        return result, 0 if result['status'] == 'ok' else 2
    if config is None:
        raise ValueError('Run init first or select a project with --config')
    from memweft import Memory
    if args.command == 'resume':
        return resume(args, config, Memory)
    with project_lock(config), Memory(str(config['database'])) as memory:
        user = memory.user(config['user'], tenant_id='handoff:' + config['project'], agent_id='project-handoff')
        if args.command == 'remember':
            if not args.value.strip() or not args.source.strip():
                raise ValueError('Fact content and source must be nonempty')
            user.remember({'content': args.value, 'source': args.source, 'recorded_at': now()}, key=PREFIX + args.key)
            return {'saved': args.key, 'writeback': target(config)}, 0
        if args.command == 'forget':
            return {'key': args.key, 'forgotten': user.forget(PREFIX + args.key), 'writeback': target(config)}, 0
        if args.command == 'list':
            return {'facts': facts_from(user.memories()), 'writeback': target(config)}, 0
        if args.task_command == 'add':
            return add_task(user, config, args), 0
        if args.task_command == 'reopen':
            return reopen_task(user, config, args), 0
        if args.task_command == 'complete':
            return complete_task(user, config, args)
        return {'tasks': [dict(task, verification={'status': freshness(task, config)}) for task in all_tasks(user)],
                'writeback': target(config)}, 0


def resume(args, config, Memory):
    from handoff import PREFIX, facts_from, now, render
    from planner import plan
    if not args.question.strip():
        raise ValueError('A nonempty task question is required')
    if args.out:
        output = args.out.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        output = new_run(config, 'resume')
    with project_lock(config), Memory(str(config['database'])) as memory:
        user = memory.user(config['user'], tenant_id='handoff:' + config['project'], agent_id='project-handoff')
        tasks = all_tasks(user)
        if args.task:
            tasks = [task for task in tasks if task['id'] == args.task]
        required = [PREFIX + key for key in dict.fromkeys(config['required_facts'] + args.require)]
        required += [TASK_PREFIX + task['id'] for task in tasks]
        if args.task and not tasks:
            required.append(TASK_PREFIX + args.task)
        context = user.session('continuation').context(query=args.question, required_fact_keys=required,
                    max_facts=args.max_facts, max_tokens=args.max_tokens, include_messages=False)
        report = context.explain()
        def label(key):
            return key.removeprefix(PREFIX) if key.startswith(PREFIX) else 'task:' + key.removeprefix(TASK_PREFIX)
        requirements = {field: [label(key) for key in report['requirements'][field]]
                        for field in ('requested', 'included', 'missing', 'excluded')}
        requirements['complete'] = report['requirements']['complete']
        included = {record.get('fact_key'): record.get('value') for record in context.memories}
        selected = [dict(task, verification={'status': freshness(task, config)}) for task in tasks
                    if TASK_PREFIX + task['id'] in included]
        records = user.memories()
        snapshot = {'schema_version': 2, 'captured_at': now(), 'project': config['project'], 'user': config['user'],
                    'question': args.question, 'facts': facts_from(context.memories), 'tasks': selected,
                    'requirements': requirements, 'context_report': report, 'writeback': target(config),
                    'status': 'ready_for_review' if requirements['complete'] else 'incomplete',
                    'human_review_status': 'pending', 'model_calls': 0, 'sdk_version': version('memweft'),
                    'app_hashes': {path.name: digest(path) for path in Path(__file__).parent.glob('*.py')}}
        if args.task:
            snapshot['focus_task'] = args.task
        check_config(config)
        write_json(output / 'snapshot.json', snapshot)
    stale = [task['id'] for task in selected if task['verification']['status'] not in ('not_run', 'passed')]
    if stale:
        planning = {'status': 'needs_context', 'reason': 'Evidence changed; inspect task list and explicitly reopen completed tasks.',
                    'stale_tasks': stale, 'model_calls': 0, 'human_review': 'pending'}
    elif args.model == 'kimi':
        planning = plan(snapshot, prompt_key=args.prompt_key)
    else:
        planning = {'status': 'not_requested', 'model_calls': 0, 'human_review': 'pending'}
    write_json(output / 'model-response.json', {'state_check': 'pending', 'unvalidated_planning': planning})
    # Paid requests run outside the CLI lock. Reject advice if its local basis changed meanwhile.
    try:
        with project_lock(config), Memory(str(config['database'])) as memory:
            user = memory.user(config['user'], tenant_id='handoff:' + config['project'], agent_id='project-handoff')
            if user.memories() != records or any(freshness(task, config) != task['verification']['status'] for task in selected):
                raise ValueError('Project state changed during planning; resume again')
    except (ValueError, OSError, RuntimeError) as error:
        planning = dict(planning, status='stale', reason=str(error), proposal=None)
    write_json(output / 'planning.json', planning)
    result = dict(snapshot, model_calls=planning['model_calls'], planning=planning,
                  snapshot_sha256=digest(output / 'snapshot.json'), output=str(output))
    write_json(output / 'attempt.json', result)
    rendered = render(snapshot).replace('这份交接由已保存的记录直接生成，未调用模型。', '当前记录由存储恢复；模型调用与提案见下方续接建议。')
    parts = [rendered, '\n## 当前任务\n']
    parts += [f"- {task['id']}: {task['title']} ({task['status']}, revision {task['revision']}; {task['verification']['status']})"
              for task in selected]
    parts += ['\n## 续接建议\n', '模型请求次数：' + str(planning['model_calls']), json.dumps(planning.get('proposal') or {'status': planning['status'], 'reason': planning.get('reason')}, ensure_ascii=False, indent=2),
              '\n任务完成需运行已登记的验收脚本；模型建议不写入完成状态。人工复核仍待进行。\n',
              '状态写回位置：' + str(config['database']) + '\n']
    with (output / 'handoff.md').open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(parts))
    blocked = not requirements['complete'] or planning['status'] in ('needs_context', 'invalid_response', 'request_failed', 'stale')
    return result, 2 if blocked else 0
