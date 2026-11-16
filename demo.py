"""Offline fixture adapters for the exported coordinator."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

from coordinator import atomic, event_commands, execute, git

TASK_ID = 'QA-017'
TASK = 'Сдвинуть кнопку под подпись на узком экране'
APP = 'sample-app/profile.js'
TEST = 'sample-app/profile.test.js'
COMPLETION = 'Ждёт ретеста'

UNFIXED = "module.exports = function layout(width) { return 'side-by-side'; };\n"
FIXED = "module.exports = function layout(width) { return width < 480 ? 'stacked' : 'side-by-side'; };\n"
REGRESSION_TEST = """const assert = require('node:assert/strict');
const test = require('node:test');
const layout = require('./profile.js');

test('uses a stacked layout on narrow screens', () => {
    assert.equal(layout(360), 'stacked');
});

test('preserves the desktop layout', () => {
    assert.equal(layout(1366), 'side-by-side');
});
"""


def source_hash(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class LocalServices:
    def __init__(self, root, base, change_row=False):
        self.root = root
        self.base = base
        self.change_row = change_row
        self.rows = [{
            'id': TASK_ID, 'status': 'В работе', 'active': True, 'unique': True,
            'values': [TASK_ID, TASK], 'source_hash': source_hash([TASK_ID, TASK]),
        }]
        self.writes = []
        self.release = None

    def sheet(self, action, request=None):
        if action == 'snapshot':
            return {'rows': copy.deepcopy(self.rows)}
        if action != 'complete':
            raise ValueError('Unsupported fixture queue operation')
        matches = [row for row in self.rows if row['id'] == request['id']]
        if len(matches) != 1 or matches[0]['source_hash'] != request['source_hash'] or not matches[0]['active']:
            raise ValueError('Fixture row changed before status write')
        matches[0].update(status=COMPLETION, active=False)
        receipt = {'id': request['id'], 'status': COMPLETION}
        self.writes.append(receipt)
        return receipt

    def deploy(self, revision):
        # The fixture has no live release. Materialize one artifact locally and
        # verify its bytes against the accepted Git revision.
        release_dir = self.root / 'release'
        release_dir.mkdir(exist_ok=True)
        expected = git(self.base, 'show', f'{revision}:{APP}').stdout.encode()
        artifact = release_dir / 'profile.js'
        artifact.write_bytes(expected)
        expected_hash = hashlib.sha256(expected).hexdigest()
        served_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        current_revision = git(self.base, 'rev-parse', 'HEAD').stdout.strip()
        self.release = {
            'confirmed': current_revision == revision and served_hash == expected_hash,
            'qa_commit': revision,
            'production_commit': current_revision,
            'confirmed_at': time.time(),
            'asset_sha256': served_hash,
        }
        if self.change_row:
            row = self.rows[0]
            row['values'].append('Новое уточнение: учесть высоту подписи.')
            row['source_hash'] = source_hash(row['values'])
        return self.release

    @staticmethod
    def telegram(_request):
        return {'ok': False, 'error': 'Fixture transport'}


def fixture_git(base):
    base.mkdir()
    git(base, 'init', '--quiet', '--template=')
    (base / APP).parent.mkdir()
    (base / APP).write_text(UNFIXED)
    (base / TEST).write_text(REGRESSION_TEST)
    git(base, 'add', '--', APP, TEST)
    git(base, '-c', 'user.name=QA Demo', '-c', 'user.email=qa@localhost', 'commit',
        '--quiet', '--no-gpg-sign', '-m', 'Synthetic QA fixture')


def worker_result(worker, node, scenario):
    directory = Path(worker['directory'])
    repo = directory / 'repo'
    if scenario != 'test-failed':
        (repo / APP).write_text(FIXED)
    command = f'node --test {TEST}'
    exit_code = execute([node, '--test', TEST], cwd=repo, check=False, timeout=30).returncode
    events = [
        {'type': 'thread.started', 'thread_id': 'fixture'},
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': command, 'exit_code': exit_code}},
        {'type': 'turn.completed'},
    ]
    (directory / 'events.jsonl').write_text('\n'.join(map(json.dumps, events)) + '\n')
    result = {
        'message': 'Synthetic worker output',
        'needed_files': [],
        'results': [{
            'id': TASK_ID, 'outcome': 'fixed', 'summary': 'Обновлена раскладка профиля',
            'checks': [{'command': command, 'exit_code': 0}],
            'decision': {'kind': 'none', 'scope': 'none', 'question': '', 'proposal': '', 'evidence': []},
        }],
    }
    atomic(directory / 'result.json', result)
    _thread, completed, commands = event_commands(directory)
    if not completed:
        raise RuntimeError('Fixture worker did not complete')
    return result, commands


def run_scenario(coordinator_type, scenario):
    if scenario not in {'ready', 'test-failed', 'row-changed'}:
        raise ValueError(f'Unknown scenario: {scenario}')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js 18+ is required for the fixture regression test.')
    with tempfile.TemporaryDirectory(prefix='qa-bot-') as temporary:
        root = Path(temporary)
        base = root / 'repo'
        fixture_git(base)
        services = LocalServices(root, base, change_row=scenario == 'row-changed')
        state_dir = root / 'state'
        state_dir.mkdir()
        coordinator = coordinator_type(state_dir, base, node, services.sheet, services.deploy, services.telegram)
        plan_dir = root / 'plan'
        plan_dir.mkdir()
        atomic(plan_dir / 'image-map.json', {})
        atomic(plan_dir / 'schema.json', json.loads(Path(__file__).with_name('senior-schema.json').read_text()))
        plan = {'directory': str(plan_dir), 'rows': services.sheet('snapshot')['rows']}
        coordinator.accept_plan(plan, {
            'blocks': [{'ids': [TASK_ID], 'files': [APP, TEST], 'title': TASK, 'reason': 'Synthetic assignment'}],
            'deferred': [],
            'note': 'Synthetic planner output',
        })
        worker = coordinator.state['jobs'][0]
        result, commands = worker_result(worker, node, scenario)
        error = None
        try:
            coordinator.accept_worker(worker, result, commands)
        except ValueError as failure:
            if scenario != 'test-failed' or worker['phase'] != 'active':
                raise
            error = str(failure)
            worker['phase'] = 'checks_failed'
            coordinator.save()
        return {
            'scenario': scenario,
            'task': TASK_ID,
            'worker_events': commands,
            'phase': worker['phase'],
            'error': error,
            'git_revision': worker.get('accepted_commit'),
            'release_verified': bool(services.release and services.release['confirmed']),
            'asset_sha256': services.release['asset_sha256'] if services.release else None,
            'status': services.rows[0]['status'],
            'status_writes': services.writes,
        }
