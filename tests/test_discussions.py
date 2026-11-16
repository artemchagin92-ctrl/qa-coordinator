import copy
import unittest

from approvals import ApprovalQueue
from discussions import DiscussionDigest, details, groups, summary


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.state = {'holds': {}, 'outbox': [], 'telegram_approvals': {'questions': [],
            'version': 1, 'sequence': 0, 'order': 0, 'active': None, 'offset': None}}
        self.calls = []
        self.now = 1000
        self.digest = DiscussionDigest(self.state, lambda: None, self.transport, lambda: self.now)

    def transport(self, request):
        self.calls.append(request)
        return {'ok': True, 'message_id': 101, 'date': self.now}

    def add(self, number):
        bug = 'QA-' + str(number)
        q = {'id': 'question-' + str(number), 'bug': bug, 'source_hash': 'hash-' + bug, 'status': 'architecture',
             'reason': 'Нужна архитектура автоматизации'}
        self.state['holds'][bug] = {'source_hash': q['source_hash'], 'reason': q['reason']}
        self.state['telegram_approvals']['questions'].append(q)
        return q

    def test_seventy_related_bugs_are_one_topic_and_one_message(self):
        for n in range(1001, 1071):
            self.add(n)
        before = copy.deepcopy(self.state['holds'])
        self.digest.publish(); self.digest.publish()
        self.assertEqual(list(groups(self.state)), ['automation'])
        self.assertEqual(len(self.calls), 1)
        self.assertIn('1 тем, 70 связанных багов', self.calls[0]['text'])
        self.assertEqual(self.state['holds'], before)
        self.assertNotIn('owner_clarifications', self.state)

    def test_additional_bug_edits_existing_message_without_new_alert(self):
        self.add(1001); self.digest.publish(); self.add(1002); self.now += 61; self.digest.publish()
        self.assertEqual([r['action'] for r in self.calls], ['send', 'edit'])
        self.assertEqual(self.calls[1]['message_id'], 101)
        self.assertIn('2 связанных багов', self.calls[1]['text'])

    def test_updates_are_batched_for_one_minute(self):
        self.add(1001); self.digest.publish(); self.add(1002); self.now += 20; self.digest.publish()
        self.assertEqual(len(self.calls), 1)
        self.now += 41; self.digest.publish(); self.assertEqual(len(self.calls), 2)

    def test_restart_reuses_message_receipt(self):
        self.add(1001); self.digest.publish(); self.add(1002); self.now += 61
        restored = copy.deepcopy(self.state)
        DiscussionDigest(restored, lambda: None, self.transport, lambda: self.now).publish()
        self.assertEqual(self.calls[-1]['action'], 'edit')
        self.assertEqual(self.calls[-1]['message_id'], 101)

    def test_resolving_architecture_removes_topic_without_claiming_fix(self):
        q = self.add(1001); self.digest.publish(); self.state['holds'].pop(q['bug']); self.now += 61
        self.digest.publish()
        self.assertEqual(groups(self.state), {})
        self.assertIn('не означает, что все исправления уже завершены', self.calls[-1]['text'])
        self.assertEqual(self.calls[-1]['action'], 'edit')

    def test_old_source_and_resolved_question_are_not_counted(self):
        q = self.add(1001); self.state['holds'][q['bug']]['source_hash'] = 'changed'
        another = self.add(1002); another['status'] = 'approved_in_codex'
        self.assertEqual(groups(self.state), {})
        self.digest.publish(); self.assertEqual(self.calls, [])

    def test_deleted_digest_is_recreated_after_backoff(self):
        self.add(1001); self.digest.publish(); self.add(1002); self.now += 61
        self.digest.transport = lambda request: {'ok': False, 'error': 'HTTP 400', 'missing_message': True}
        self.digest.publish(); self.assertNotIn('message_id', self.digest.data)
        self.digest.transport = self.transport; self.digest.publish(); self.assertEqual(len(self.calls), 1)
        self.now += 61; self.digest.publish(); self.assertEqual(self.calls[-1]['action'], 'send')

    def test_failed_edit_keeps_receipt_and_retries_edit(self):
        self.add(1001); self.digest.publish(); self.add(1002); self.now += 61
        self.digest.transport = lambda request: {'ok': False, 'error': 'HTTP 502'}
        self.digest.publish(); self.assertEqual(self.digest.data['message_id'], 101)
        self.digest.transport = self.transport; self.now += 61; self.digest.publish()
        self.assertEqual(self.calls[-1]['action'], 'edit')

    def test_details_include_all_ids_with_bounded_telegram_size(self):
        for n in range(1001, 1071):
            self.add(n)
        text = details(self.state, 'automation')
        self.assertIn('QA-1001', text); self.assertIn('QA-1070', text)
        self.assertLessEqual(len(text), 3900)
        self.assertIn('Общего подтверждения', text)

    def test_queue_commands_show_topics_and_never_approve(self):
        self.add(1001); self.add(1002)
        queue = ApprovalQueue(self.state, lambda: None, self.transport, lambda: {'rows': []})
        for command in ['/queue', '/decisions', '/topic automation']:
            queue.handle({'authorized': True, 'text': command})
        self.assertIn('1 тем, 2 багов', self.state['outbox'][0]['text'])
        self.assertIn('Автоматизация', self.state['outbox'][1]['text'])
        self.assertIn('QA-1002', self.state['outbox'][2]['text'])
        self.assertEqual(len(self.state['holds']), 2)

    def test_architecture_enqueue_does_not_emit_per_bug_notifications(self):
        self.state.pop('telegram_approvals')
        queue = ApprovalQueue(self.state, lambda: None, self.transport, lambda: {'rows': []})
        for number in range(1001, 1011):
            row = {'id': 'QA-' + str(number), 'source_hash': 'hash', 'values': []}
            queue.enqueue(row, 'Нужна архитектура функции')
        self.assertEqual(len(self.state['telegram_approvals']['questions']), 10)
        self.assertEqual(self.state['outbox'], [])


if __name__ == '__main__':
    unittest.main()
