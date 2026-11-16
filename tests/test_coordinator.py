import unittest
from unittest.mock import Mock

from coordinator import Coordinator
from demo import APP, TASK_ID, run_scenario


class LocalFlow(unittest.TestCase):
    def test_passing_command_event_integrates_and_records_retest(self):
        result = run_scenario(Coordinator, 'ready')
        self.assertEqual(result['worker_events'][0]['exit_code'], 0)
        self.assertEqual(result['phase'], 'done')
        self.assertRegex(result['git_revision'], r'^[0-9a-f]{40,64}$')
        self.assertTrue(result['release_verified'])
        self.assertRegex(result['asset_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(result['status_writes'], [{'id': TASK_ID, 'status': 'Ждёт ретеста'}])

    def test_claimed_pass_without_successful_command_event_stops_work(self):
        result = run_scenario(Coordinator, 'test-failed')
        self.assertEqual(result['worker_events'][0]['exit_code'], 1)
        self.assertEqual(result['phase'], 'checks_failed')
        self.assertIn('command_execution evidence', result['error'])
        self.assertIsNone(result['git_revision'])
        self.assertFalse(result['release_verified'])
        self.assertEqual(result['status'], 'В работе')
        self.assertEqual(result['status_writes'], [])

    def test_changed_source_hash_preserves_status(self):
        result = run_scenario(Coordinator, 'row-changed')
        self.assertEqual(result['worker_events'][0]['exit_code'], 0)
        self.assertTrue(result['release_verified'])
        self.assertEqual(result['phase'], 'status_conflict')
        self.assertEqual(result['status'], 'В работе')
        self.assertEqual(result['status_writes'], [])

    def test_plan_rejects_paths_reserved_by_active_worker(self):
        coordinator = Mock(state={'jobs': [
            {'role': 'worker', 'phase': 'active', 'files': [APP], 'rows': [{'id': 'QA-018'}]},
        ]})
        job = {'rows': [{'id': TASK_ID}]}
        plan = {'blocks': [{'ids': [TASK_ID], 'files': [APP]}]}

        with self.assertRaisesRegex(ValueError, 'non-independent or invalid block assignment'):
            Coordinator.accept_plan(coordinator, job, plan)
        coordinator.create_job.assert_not_called()

    def test_confirmed_release_with_wrong_revision_cannot_read_or_update_sheet(self):
        coordinator = Mock()
        coordinator.deploy.return_value = {'confirmed': True, 'qa_commit': 'previous'}
        job = {'accepted_commit': 'requested', 'title': 'Synthetic task'}

        Coordinator.report(coordinator, job)

        self.assertIn('release not confirmed', job['deploy_error'])
        coordinator.sheet.assert_not_called()
