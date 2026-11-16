"""QA coordinator extracted from the original Python service.

The command-line entry point runs the same validation and state transitions
against a disposable local workspace. Service transports are injected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import uuid

from approvals import ApprovalQueue

BUSY = {'active', 'launching', 'model_retry', 'launch_error', 'waiting_files', 'integrating', 'pending_status'}
MAX_WORKERS = 2

def atomic(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    os.replace(temporary, path)

def execute(args, cwd=None, data=None, timeout=120, check=True):
    # Do not let personal Git configuration, credential helpers, or Node
    # preload options influence work in the fixture repositories.
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith('GIT_') and key not in {'NODE_OPTIONS', 'NODE_PATH'}
    }
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(
        args, cwd=cwd, input=data, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if check and result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} exit {result.returncode}: {result.stderr[-700:]}')
    return result


def git(repo, *args, data=None, check=True):
    return execute(
        ['git', '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false',
         '-C', str(repo), *args],
        data=data, check=check,
    )

def permitted(path):
    p = Path(path)
    return (not p.is_absolute() and len(p.parts) >= 2 and p.parts[0] == 'sample-app'
            and not any(x in {'..', '.git', 'node_modules', 'data', 'uploads', 'logs', '.ssh', '.codex'}
                        or x.startswith('.env') or x.endswith(('.db', '.sqlite', '.sqlite3')) for x in p.parts))

def event_commands(attempt):
    commands = []
    complete = False
    thread = None
    for line in (attempt / 'events.jsonl').read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'thread.started':
            thread = event.get('thread_id')
        if event.get('type') == 'turn.completed':
            complete = True
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
            commands.append({'command': item.get('command'), 'exit_code': item.get('exit_code')})
    return thread, complete, commands

def canonical_command(command):
    """Match a reported command to the shell-wrapped command in real CLI events."""
    try:
        parts = shlex.split(command)
        if parts and Path(parts[0]).name in {'bash', 'zsh', 'sh', 'dash'}:
            for index, part in enumerate(parts[1:], 1):
                if part in {'-c', '-lc', '-cl'} and index + 1 < len(parts):
                    return canonical_command(parts[index + 1])
        return shlex.join(parts)
    except ValueError:
        return command.strip()

class Coordinator:
    def __init__(self, state_dir, base, node, sheet, deploy, telegram):
        self.state_dir = state_dir
        self.base = base
        self.node = node
        self.sheet = sheet
        self.deploy = deploy
        self.telegram = telegram
        self.path = state_dir / 'state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'version': 1, 'jobs': [], 'holds': {}, 'outbox': [],
        }
        self.save()

    def approvals(self):
        return ApprovalQueue(self.state, self.save, self.telegram, lambda: self.sheet('snapshot'))

    def create_job(self, role, rows, files, title, image_map):
        job_id = uuid.uuid4().hex
        directory = self.state_dir / 'jobs' / job_id
        directory.mkdir(parents=True, mode=0o700)
        git(self.base, 'clone', '--no-hardlinks', str(self.base), str(directory / 'repo'))
        git(directory / 'repo', 'remote', 'remove', 'origin')
        request = directory / 'qa-input'
        request.mkdir()
        atomic(request / 'rows.json', rows)
        shutil.copyfile(Path(__file__).with_name('worker-schema.json'), directory / 'schema.json')
        job = {
            'id': job_id, 'role': role, 'directory': str(directory), 'rows': rows,
            'images': image_map, 'files': files, 'title': title, 'phase': 'active',
            'base': git(self.base, 'rev-parse', 'HEAD').stdout.strip(),
        }
        self.state['jobs'].append(job)
        self.save()
        return job

    def save(self):
        self.state['updated'] = time.time()
        atomic(self.path, self.state)

    def notify(self, message):
        self.state['outbox'].append({'time': time.time(), 'text': message, 'sent': False})
        self.save()

    def decision_evidence(self, row, decision, job=None):
        evidence = (decision or {}).get('evidence', [])
        if not evidence or len(evidence) > 4 or not job:
            return False
        for item in evidence:
            kind, ref, quote = item.get('type'), item.get('reference', ''), item.get('quote', '')
            if len(quote) < 8:
                return False
            if kind == 'qa_row':
                if ref != row['id'] or not any(quote in str(v) for v in row['values']):
                    return False
            elif kind == 'code':
                if not permitted(ref):
                    return False
                source = git(self.base, 'show', 'HEAD:' + ref, check=False)
                if source.returncode or quote not in source.stdout:
                    return False
            elif kind == 'requirement':
                path = self.state_dir / 'requirements.json'
                if not path.is_file():
                    return False
                def entries(value):
                    if isinstance(value, dict):
                        if isinstance(value.get('values'), list):
                            yield value['values']
                        for v in value.values():
                            yield from entries(v)
                    elif isinstance(value, list):
                        for v in value:
                            yield from entries(v)
                if not any(values and str(values[0]).strip() == ref
                           and any(quote in str(v) for v in values)
                           for values in entries(json.loads(path.read_text()))):
                    return False
            elif kind == 'image':
                names = job.get('images', {}).get(row['id'], [])
                if Path(ref).name not in names or Path(ref).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
                    return False
            else:
                return False
        return True

    def block_row(self, row, reason, decision=None, job=None):
        file_wait = any(word in reason.lower() for word in
                        ('занят', 'освобожд', 'reserved', 'file conflict', 'busy file'))
        dependencies = [j['id'] for j in self.state['jobs'] if file_wait and j['id'] in reason]
        if file_wait and not dependencies:
            dependencies = [j['id'] for j in self.state['jobs']
                            if j['role'] == 'worker' and j['phase'] in BUSY]
        hold = {'source_hash': row['source_hash'], 'reason': reason}
        if file_wait:
            hold.update(kind='internal_dependency', wait_for=dependencies)
        self.state['holds'][row['id']] = hold
        if not file_wait:
            self.approvals().enqueue(row, reason, decision, self.decision_evidence(row, decision, job))
        self.save()

    def accept_plan(self, job, result):
        rows = {r['id']: r for r in job['rows']}
        active = [j for j in self.state['jobs'] if j['role'] == 'worker' and j['phase'] in BUSY]
        reserved = set(p for j in active for p in j['files'])
        used = set(r['id'] for j in active for r in j['rows'])
        blocks = result['blocks']
        if len(blocks) > MAX_WORKERS - len(active):
            raise ValueError('too many simultaneous blocks')
        for block in blocks:
            ids, files = block['ids'], block['files']
            if (not ids or not files or len(ids) != len(set(ids)) or len(files) != len(set(files))
                    or any(bug not in rows or bug in used for bug in ids)
                    or any(not permitted(p) or p in reserved for p in files)):
                raise ValueError('non-independent or invalid block assignment')
            used.update(ids)
            reserved.update(files)
        job['phase'] = 'planned'
        job['plan'] = result
        self.save()
        image_map = json.loads((Path(job['directory']) / 'image-map.json').read_text())
        for block in blocks:
            self.create_job('worker', [rows[x] for x in block['ids']], block['files'], block['title'], image_map)
        for item in result['deferred']:
            if item['id'] in rows and item['id'] not in used:
                self.block_row(rows[item['id']], item['reason'], item.get('decision'), job)
        self.state['next_plan'] = time.time() + (30 if blocks else 300)
        self.save()

    def accept_worker(self, job, result, commands):
        ids = {r['id'] for r in job['rows']}
        records = result['results']
        if len(records) != len(ids) or {r['id'] for r in records} != ids:
            raise ValueError('worker must report every assigned ID exactly once')
        if any(r['outcome'] == 'needs_revision' for r in records):
            needed = result.get('needed_files', [])
            if needed and all(permitted(p) for p in needed):
                job['needed_files'] = needed
                job['phase'] = 'waiting_files'
                job['revision_reason'] = result['message'] + ' ' + ' '.join(r['summary'] for r in records)
                self.save()
                return
            raise ValueError('worker requests revision: ' + result['message'] + ' ' + ' '.join(r['summary'] for r in records))
        fixed = [r for r in records if r['outcome'] == 'fixed']
        if not fixed:
            for record in records:
                row = next(r for r in job['rows'] if r['id'] == record['id'])
                self.block_row(row, record['summary'], record.get('decision'), job)
            job['phase'] = 'needs_owner'
            job['error'] = ' '.join(r['summary'] for r in records)
            self.save()
            return
        for record in fixed:
            checks = record['checks']
            if not checks or any(c['exit_code'] != 0 or not any(
                    canonical_command(e['command'] or '') == canonical_command(c['command'])
                    and e['exit_code'] == 0 for e in commands) for c in checks):
                raise ValueError('fixed requires real successful command_execution evidence for its declared checks')
        repo = Path(job['directory']) / 'repo'
        # Include permitted new files; never qa-input, caches or node_modules.
        new = git(repo, 'ls-files', '--others', '--exclude-standard', '-z', '--', 'sample-app').stdout.split('\0')
        for name in filter(None, new):
            if name not in job['files']:
                raise ValueError('unassigned new file: ' + name)
            git(repo, 'add', '--intent-to-add', '--', name)
        names = [p for p in git(repo, 'diff', '--name-only', '-z', job['base']).stdout.split('\0') if p]
        if not names or any(p not in job['files'] or not permitted(p) for p in names):
            raise ValueError('empty diff or changes outside assigned files')
        for name in names:
            path = repo / name
            if path.is_symlink():
                raise ValueError('symlink changes are not accepted')
            if path.is_file() and path.suffix == '.js':
                syntax = execute([self.node, '--check', str(path)], check=False)
                if syntax.returncode:
                    raise ValueError('changed JavaScript syntax failed: ' + name + ' ' + syntax.stderr[-400:])
        patch = git(repo, 'diff', '--binary', '--no-ext-diff', '--no-textconv', job['base']).stdout
        directory = self.state_dir / 'results' / job['id']
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (directory / 'fix.patch').write_text(patch)
        atomic(directory / 'result.json', result)
        job['result_directory'] = str(directory)
        job['fixed_ids'] = [r['id'] for r in fixed]
        job['reported_ids'] = []
        job['phase'] = 'integrating'
        job['integration_base'] = git(self.base, 'rev-parse', 'HEAD').stdout.strip()
        job['patch_sha256'] = hashlib.sha256(patch.encode()).hexdigest()
        self.save()
        self.integrate(job)
        for record in records:
            if record['outcome'] == 'blocked':
                row = next(r for r in job['rows'] if r['id'] == record['id'])
                self.block_row(row, record['summary'], record.get('decision'), job)
        self.save()
        self.report(job)

    def integrate(self, job):
        """Reconcile an already-applied/committed QA patch after a coordinator crash."""
        patch = (Path(job['result_directory']) / 'fix.patch').read_text()
        head = git(self.base, 'rev-parse', 'HEAD').stdout.strip()
        marker = 'QA block ' + job['id']
        if head != job['integration_base']:
            if marker not in git(self.base, 'log', '-1', '--format=%B').stdout:
                raise ValueError('QA integration HEAD changed unexpectedly')
            job['accepted_commit'] = head
            job['phase'] = 'pending_status'
            self.save()
            return
        staged = git(self.base, 'diff', '--cached', '--binary').stdout
        if staged:
            if hashlib.sha256(staged.encode()).hexdigest() != job['patch_sha256']:
                raise ValueError('unrelated staged changes; QA progress preserved')
        else:
            check = git(self.base, 'apply', '--check', '--index', '-', data=patch, check=False)
            if check.returncode:
                job['phase'] = 'active'
                self.save()
                raise ValueError('QA integration conflict: ' + check.stderr[-500:])
            git(self.base, 'apply', '--index', '-', data=patch)
        message = 'QA: ' + ', '.join(job['fixed_ids']) + '\n\n' + marker
        git(self.base, '-c', 'user.name=QA Demo', '-c', 'user.email=qa@localhost',
            'commit', '--no-gpg-sign', '-m', message)
        job['accepted_commit'] = git(self.base, 'rev-parse', 'HEAD').stdout.strip()
        job['phase'] = 'pending_status'
        self.save()

    def report(self, job):
        if not job.get('production'):
            if time.time() < job.get('deploy_retry_after', 0):
                return
            try:
                receipt = self.deploy(job['accepted_commit'])
                if not receipt.get('confirmed') or receipt.get('qa_commit') != job['accepted_commit']:
                    raise ValueError('release not confirmed for accepted revision')
                job['production'] = {k: receipt[k] for k in ['production_commit', 'qa_commit', 'confirmed_at']}
                job.pop('deploy_error', None)
                self.save()
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                job['deploy_error'] = str(error)[-1200:]
                job['deploy_retry_after'] = time.time() + 300
                self.save()
                if not job.get('deploy_failure_notified'):
                    job['deploy_failure_notified'] = True
                    self.notify('QA: исправления сохранены, но production-выкладка пока не подтверждена. Статус не изменён. Блок: ' + job['title'] + '\n' + job['deploy_error'])
                return
        current = self.sheet('snapshot')['rows']
        for bug in job['fixed_ids']:
            if bug in job['reported_ids']:
                continue
            original = next(r for r in job['rows'] if r['id'] == bug)
            matches = [r for r in current if r['id'] == bug]
            if len(matches) != 1 or matches[0]['source_hash'] != original['source_hash']:
                self.state['holds'][bug] = {'source_hash': original['source_hash'], 'reason': 'QA input changed after fix'}
                job['phase'] = 'status_conflict'
                self.save()
                self.notify('QA: ' + bug + ' исправлен в QA-копии, но вход таблицы изменился. Статус не перезаписан.')
                return
            row = matches[0]
            if not row['active'] and row['status'].strip().casefold() != 'ждёт ретеста':
                self.state['holds'][bug] = {'source_hash': original['source_hash'], 'reason': 'manual terminal status already set'}
                job['phase'] = 'status_conflict'
                self.save()
                self.notify('QA: ' + bug + ' исправлен в QA-копии; ручной статус таблицы сохранён: ' + row['status'])
                return
            if row['status'].strip().casefold() != 'ждёт ретеста':
                evidence = self.sheet('complete', {'id': bug, 'source_hash': original['source_hash']})
                job.setdefault('status_writes', []).append(evidence)
            job['reported_ids'].append(bug)
            self.save()
        job['phase'] = 'done'
        self.save()
        self.notify('QA: ' + ', '.join(job['reported_ids']) + ' — ждёт ретеста.\n'
                    'Целевые проверки пройдены. Подтверждён локальный релиз '
                    + job['production']['production_commit'][:12] + '.')

    def refresh_internal_dependencies(self):
        busy_ids = {job['id'] for job in self.state['jobs'] if job['phase'] in BUSY or job['phase'] == 'preparing'}
        released = [bug for bug, hold in self.state['holds'].items()
                    if hold.get('kind') == 'internal_dependency'
                    and not any(job_id in busy_ids for job_id in hold['wait_for'])]
        if released:
            for bug in released:
                del self.state['holds'][bug]
            self.state['next_plan'] = 0
            self.save()

def main():
    from demo import run_scenario

    parser = argparse.ArgumentParser(description='Run the QA coordinator in an isolated local fixture.')
    parser.add_argument('--scenario', choices=['ready', 'test-failed', 'row-changed'], required=True)
    args = parser.parse_args()
    print(json.dumps(run_scenario(Coordinator, args.scenario), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
