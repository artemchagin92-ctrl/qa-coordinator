import copy
import json
import tempfile
import unittest
from pathlib import Path

from approvals import ApprovalQueue, classify
from notify import owner_update


class Flow(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.state = {'holds': {}, 'jobs': [], 'outbox': []}
        self.rows = []
        self.calls = []
        self.updates = []
        self.mid = 100
        self.file = tempfile.TemporaryDirectory()
        self.path = Path(self.file.name) / 'state.json'
        self.addCleanup(self.file.cleanup)
        self.queue = ApprovalQueue(self.state, self.save, self.transport, self.snapshot, lambda: self.now)

    def save(self):
        self.path.write_text(json.dumps(self.state))

    def snapshot(self):
        return {'rows': copy.deepcopy(self.rows)}

    def transport(self, request):
        self.calls.append(request)
        if request['action'] == 'poll':
            updates, self.updates = self.updates, []
            return {'ok': True, 'updates': updates}
        if request['action'] == 'send':
            self.mid += 1
            return {'ok': True, 'message_id': self.mid, 'date': self.now}
        return {'ok': True}

    def add(self, bug='BUG-UI-1', decision=None, reason='Противоречие подписи и примера'):
        row = {'id': bug, 'source_hash': 'hash-' + bug, 'active': True, 'unique': True,
               'values': [bug, 'Выделять выбранную вкладку настроек']}
        self.rows.append(row)
        self.state['holds'][bug] = {'source_hash': row['source_hash'], 'reason': reason}
        decision = decision or {'kind': 'simple', 'scope': 'ui_style',
            'question': 'Исправлять выделение вкладки настроек?',
            'proposal': 'После переключения вкладки обновить класс активного элемента.',
            'evidence': [{'type': 'qa_row', 'reference': bug, 'quote': row['values'][1]}]}
        return self.queue.enqueue(row, reason, decision, True)

    def answer(self, text='да', uid=1, reply=None, date=None):
        self.updates = [{'update_id': uid, 'authorized': True, 'text': text,
            'message_id': self.mid + 100, 'date': self.now + 1 if date is None else date,
            'reply_to_message_id': reply}]
        self.queue.poll()

    def test_approval_releases_exact_bug_and_binds_proposal(self):
        q = self.add(); other = self.add('BUG-UI-2'); self.queue.display()
        self.answer()
        self.assertEqual(q['status'], 'approved')
        self.assertNotIn(q['bug'], self.state['holds'])
        self.assertIn(other['bug'], self.state['holds'])
        c = self.state['owner_clarifications'][q['bug']]
        self.assertIn(q['proposal'], c['text'])
        self.assertEqual(c['source_hash'], q['source_hash'])
        self.assertEqual(self.state['next_plan'], 0)

    def test_one_current_question_and_remaining_count(self):
        first = self.add(); second = self.add('BUG-UI-2')
        self.queue.display(); self.queue.display()
        sends = [x for x in self.calls if x['action'] == 'send']
        self.assertEqual(len(sends), 1)
        self.assertIn('Ещё вопросов в очереди: 1', sends[0]['text'])
        self.assertEqual(first['status'], 'awaiting'); self.assertEqual(second['status'], 'queued')

    def test_duplicate_delivery_does_not_approve_next_question(self):
        first = self.add(); second = self.add('BUG-UI-2'); self.queue.display()
        self.answer(uid=10); self.now += 10; self.queue.display(); self.answer(uid=10)
        self.assertEqual(first['status'], 'approved'); self.assertEqual(second['status'], 'awaiting')

    def test_two_yes_in_same_poll_cannot_confirm_two_questions(self):
        self.add(); second = self.add('BUG-UI-2'); self.queue.display()
        self.updates = [{'update_id': i, 'authorized': True, 'text': 'да',
                         'message_id': 200 + i, 'date': self.now + 1} for i in [1, 2]]
        self.queue.poll(); self.queue.display()
        self.assertEqual(second['status'], 'awaiting')
        self.assertIn(second['bug'], self.state['holds'])

    def test_reply_to_another_message_rejected(self):
        q = self.add(); self.queue.display(); self.answer(reply=42)
        self.assertEqual(q['status'], 'awaiting'); self.assertIn(q['bug'], self.state['holds'])

    def test_old_yes_before_question_rejected(self):
        q = self.add(); self.queue.display(); self.answer(date=self.now - 50)
        self.assertEqual(q['status'], 'awaiting')

    def test_same_second_bare_yes_ambiguous_but_explicit_reply_allowed(self):
        self.add(); second = self.add('BUG-UI-2'); self.queue.display()
        self.answer(date=self.now + 1); self.now += 1; self.queue.display()
        self.answer(uid=2, date=self.now)
        self.assertEqual(second['status'], 'awaiting')
        self.answer(uid=3, reply=second['receipts'][-1]['message_id'], date=self.now)
        self.assertEqual(second['status'], 'approved')

    def test_unauthorized_reply_ignored_and_cursor_advanced(self):
        q = self.add(); self.queue.display()
        self.updates = [{'update_id': 3, 'authorized': False}]; self.queue.poll()
        self.assertEqual(q['status'], 'awaiting'); self.assertEqual(self.queue.data['offset'], 4)

    def test_valid_button_approves_and_is_acknowledged(self):
        q = self.add(); self.queue.display()
        self.updates = [{'update_id': 2, 'authorized': True, 'callback_id': 'cb',
            'callback_data': 'qa:' + q['id'] + ':yes', 'message_id': q['receipts'][0]['message_id'],
            'date': self.now}]
        self.queue.poll()
        self.assertEqual(q['status'], 'approved')
        self.assertEqual(self.calls[-1]['action'], 'answer')

    def test_forged_callback_wrong_question_or_message_rejected(self):
        q = self.add(); self.queue.display()
        for uid, data, mid in [(1, 'qa:other:yes', self.mid), (2, 'qa:' + q['id'] + ':yes', 42)]:
            self.updates = [{'update_id': uid, 'authorized': True, 'callback_id': str(uid),
                            'callback_data': data, 'message_id': mid, 'date': self.now}]
            self.queue.poll()
        self.assertEqual(q['status'], 'awaiting')

    def test_changed_source_hash_invalidates_approval(self):
        q = self.add(); self.queue.display(); self.rows[0]['source_hash'] = 'changed'; self.answer()
        self.assertEqual(q['status'], 'stale'); self.assertIn(q['bug'], self.state['holds'])
        self.assertNotIn('owner_clarifications', self.state)

    def test_duplicate_or_inactive_row_invalidates(self):
        q = self.add(); self.queue.display(); self.rows.append(copy.deepcopy(self.rows[0])); self.answer()
        self.assertEqual(q['status'], 'stale')

    def test_external_clarification_invalidates_pending_question(self):
        q = self.add(); self.queue.display(); self.state['holds'].pop(q['bug']); self.answer()
        self.assertEqual(q['status'], 'stale')

    def test_source_failure_does_not_consume_answer(self):
        q = self.add(); self.queue.display()
        def fail():
            raise TimeoutError()
        self.queue.snapshot = fail
        with self.assertRaises(TimeoutError):
            self.answer(uid=9)
        self.assertEqual(q['status'], 'awaiting'); self.assertIsNone(self.queue.data['offset'])

    def test_no_and_discuss_keep_hold(self):
        for answer, status in [('нет', 'rejected'), ('обсудить', 'discuss')]:
            self.now += 10
            q = self.add('BUG-' + status); self.queue.display(); self.answer(answer, uid=len(self.rows))
            self.assertEqual(q['status'], status); self.assertIn(q['bug'], self.state['holds'])

    def test_later_moves_to_end_without_immediate_repeat(self):
        first = self.add(); second = self.add('BUG-UI-2'); self.queue.display()
        self.answer('позже'); self.queue.display()
        self.assertEqual(first['status'], 'queued'); self.assertEqual(second['status'], 'awaiting')
        self.assertGreater(first['order'], second['order'])

    def test_next_resumes_deferred_question(self):
        q = self.add(); self.queue.display(); self.answer('позже'); self.queue.display()
        self.assertIsNone(self.queue.current()); self.answer('/next', uid=2); self.queue.display()
        self.assertEqual(q['status'], 'awaiting')

    def test_restart_preserves_question_and_update_cursor(self):
        q = self.add(); self.queue.display(); self.answer('/queue', uid=5)
        restored = json.loads(self.path.read_text())
        queue = ApprovalQueue(restored, lambda: None, self.transport, self.snapshot, lambda: self.now)
        self.assertEqual(queue.current()['id'], q['id']); self.assertEqual(queue.data['offset'], 6)
        queue.display()
        self.assertEqual(len([x for x in self.calls if x['action'] == 'send']), 1)

    def test_failed_send_retries_without_accepting_bare_yes(self):
        q = self.add()
        self.queue.transport = lambda request: {'ok': False, 'error': 'HTTP 502'}
        self.queue.display(); self.queue.transport = self.transport; self.answer()
        self.assertEqual(q['status'], 'sending'); self.assertIn(q['bug'], self.state['holds'])
        self.now += 61; self.queue.display(); self.assertEqual(q['status'], 'awaiting')

    def test_restart_after_send_crash_resends_without_approval(self):
        q = self.add(); q['status'] = 'sending'; self.queue.data['active'] = q['id']; self.save()
        self.queue.display(); self.assertEqual(q['status'], 'awaiting')
        self.assertEqual(len(q['receipts']), 1)

    def test_architecture_cannot_be_approved_with_yes(self):
        q = self.add(reason='Нужны права доступа к списку записей')
        self.queue.display(); self.answer()
        self.assertEqual(q['status'], 'architecture'); self.assertIn(q['bug'], self.state['holds'])
        self.assertFalse(any(c['action'] == 'send' for c in self.calls))

    def test_unstructured_old_worker_result_is_architecture(self):
        row = {'id': 'BUG-OLD', 'source_hash': 'old', 'values': []}
        q = self.queue.enqueue(row, 'Нужно уточнение владельца')
        self.assertEqual(q['status'], 'architecture')

    def test_enqueuing_same_pending_bug_is_idempotent(self):
        q = self.add(); row = self.rows[0]
        again = self.queue.enqueue(row, 'different text')
        self.assertEqual(q['id'], again['id']); self.assertEqual(len(self.queue.data['questions']), 1)

    def test_risky_decision_not_allowed_even_if_model_labels_simple(self):
        row = {'values': []}
        d = {'kind': 'simple', 'scope': 'existing_behavior', 'question': 'Подтвердить изменение?',
             'proposal': 'Сделать переключатель разрешения доступа по умолчанию включённым.',
             'evidence': ['requirement']}
        self.assertEqual(classify(row, 'Права доступа пользователей', d, True), 'architecture')
        self.assertEqual(classify(row, 'Уточнение цвета', d, False), 'architecture')


class Authorization(unittest.TestCase):
    def test_only_owner_private_chat_is_accepted(self):
        message = {'message_id': 12, 'date': 100, 'text': 'да',
                   'from': {'id': 7}, 'chat': {'id': 7, 'type': 'private'}}
        self.assertTrue(owner_update({'update_id': 1, 'message': message}, 7)['authorized'])
        for sender, chat, kind in [(8, 7, 'private'), (7, 8, 'private'), (7, 7, 'group')]:
            m = copy.deepcopy(message); m['from']['id'] = sender; m['chat'].update(id=chat, type=kind)
            out = owner_update({'update_id': 1, 'message': m}, 7)
            self.assertFalse(out['authorized']); self.assertNotIn('text', out)

    def test_callback_sender_is_checked_separately_from_bot_message(self):
        msg = {'message_id': 12, 'date': 100, 'from': {'id': 999},
               'chat': {'id': 7, 'type': 'private'}}
        cb = {'id': 'abc', 'from': {'id': 7}, 'message': msg, 'data': 'qa:id:yes'}
        self.assertTrue(owner_update({'update_id': 1, 'callback_query': cb}, 7)['authorized'])
        cb['from']['id'] = 8
        self.assertFalse(owner_update({'update_id': 2, 'callback_query': cb}, 7)['authorized'])


if __name__ == '__main__':
    unittest.main()
