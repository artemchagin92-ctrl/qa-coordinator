"""Durable, owner-only Telegram decisions; no model-generated commands are executed."""
from __future__ import annotations

import re
import time
import uuid

from discussions import DiscussionDigest, details, groups, summary

YES = {'да', 'ок', 'ok', 'подтверждаю', 'подверждаю', 'согласен', 'согласна'}
NO = {'нет', 'отклоняю'}
LATER = {'позже', 'отложить'}
DISCUSS = {'обсудить', 'обсудить здесь', 'к codex'}
PENDING = {'queued', 'sending', 'awaiting'}
RISK = re.compile(
    r'архитект|регистрац|авторизац|аутентиф|парол|sms|смс|провайдер|интеграц|'
    r'плат[её]ж|оплат|биллинг|миграци|удален[иея]|удалени|удалять|'
    r'прав[ао] доступ|разрешени|приватн|конфиденц|персональн|крипт|шифрован|'
    r'нов[а-я]* функц|источник данных|адресн[а-я]* книг|импорт контакт|'
    r'permission|authentication|authorization|credential|schema|database|'
    r'privacy|encryption|payment|external integration|new feature|'
    r'server/|auth\.js|db\.js|socket\.js|баз[ауы] данных|серверн[а-я]* хран', re.I)


def classify(row, reason, decision, evidence_valid=False):
    """Only a concrete, evidenced, bounded UI decision may use a short approval."""
    d = decision or {}
    text = ' '.join([reason, d.get('question', ''), d.get('proposal', ''),
                     str(d.get('evidence', [])),
                     *[str(v) for v in row.get('values', [])]])
    simple = (d.get('kind') == 'simple'
              and d.get('scope') in {'ui_text', 'ui_style', 'existing_behavior'}
              and 8 <= len(d.get('question', '')) <= 500
              and 12 <= len(d.get('proposal', '')) <= 1500
              and bool(d.get('evidence')) and evidence_valid
              and not RISK.search(text))
    return 'simple' if simple else 'architecture'


class ApprovalQueue:
    def __init__(self, state, save, transport, snapshot, clock=time.time):
        self.state, self.save, self.transport = state, save, transport
        self.snapshot, self.clock = snapshot, clock
        self.data = state.setdefault('telegram_approvals', {
            'version': 1, 'sequence': 0, 'order': 0, 'questions': [],
            'active': None, 'offset': None})

    def notice(self, text):
        self.state.setdefault('outbox', []).append(
            {'time': self.clock(), 'text': text, 'sent': False})

    def enqueue(self, row, reason, decision=None, evidence_valid=False):
        for q in self.data['questions']:
            if (q['bug'] == row['id'] and q['source_hash'] == row['source_hash']
                    and q['status'] in PENDING | {'architecture', 'discuss', 'rejected'}):
                return q
        kind = classify(row, reason, decision, evidence_valid)
        d = decision or {}
        self.data['sequence'] += 1
        self.data['order'] += 1
        q = {'id': uuid.uuid4().hex, 'number': self.data['sequence'],
             'bug': row['id'], 'source_hash': row['source_hash'], 'reason': reason,
             'question': d.get('question', '') if kind == 'simple' else '',
             'proposal': d.get('proposal', '') if kind == 'simple' else '',
             'evidence': d.get('evidence', []), 'kind': kind,
             'status': 'queued' if kind == 'simple' else 'architecture',
             'order': self.data['order'], 'created': self.clock(), 'receipts': []}
        self.data['questions'].append(q)
        # Architecture questions are persisted per bug but announced in one topic digest.
        self.save()
        return q

    def current(self):
        return next((q for q in self.data['questions']
                     if q['id'] == self.data.get('active') and q['status'] in PENDING), None)

    def publish_discussions(self):
        DiscussionDigest(self.state, self.save, self.transport, self.clock).publish()

    def fresh(self, q):
        rows = [r for r in self.snapshot()['rows'] if r['id'] == q['bug']]
        hold = self.state.get('holds', {}).get(q['bug'], {})
        return (len(rows) == 1 and rows[0].get('unique') and rows[0].get('active')
                and rows[0]['source_hash'] == q['source_hash']
                and hold.get('source_hash') == q['source_hash'])

    def stale(self, q):
        q['status'] = 'stale'
        if self.data.get('active') == q['id']:
            self.data['active'] = None
        self.notice(f"Вопрос №{q['number']} · {q['bug']} больше не актуален: "
                    'вход задачи или её состояние изменились. Старое подтверждение не применяется.')
        self.save()

    def poll(self):
        if self.data.get('poll_after', 0) > self.clock():
            return
        result = self.transport({'action': 'poll', 'offset': self.data.get('offset')})
        if not result.get('ok'):
            self.data['poll_after'] = self.clock() + 60
            self.data['last_error'] = result.get('error', 'telegram unavailable')
            self.save()
            return
        self.data.pop('last_error', None)
        self.data['last_poll'] = self.clock()
        for update in sorted(result.get('updates', []), key=lambda u: u['update_id']):
            if self.data.get('offset') is not None and update['update_id'] < self.data['offset']:
                continue
            # Commit cursor together with the decision, before any Telegram acknowledgement.
            self.handle(update)
            self.data['offset'] = update['update_id'] + 1
            self.save()
            if update.get('callback_id'):
                self.transport({'action': 'answer', 'callback_id': update['callback_id']})
        self.save()

    def handle(self, u):
        if not u.get('authorized'):
            return
        text = re.sub(r'[.!?\s]+$', '', u.get('text', '').strip().lower())
        callback = u.get('callback_data', '')
        q = self.current()
        if not callback and text in {'/decisions', '/topics', 'темы'}:
            self.notice(summary(self.state))
            return
        if not callback and text.startswith('/topic '):
            self.notice(details(self.state, text.split(maxsplit=1)[1]))
            return
        if not callback and text in {'/queue', '/questions', 'вопросы', 'очередь'}:
            pending = sorted((x for x in self.data['questions'] if x['status'] in PENDING),
                             key=lambda x: x['order'])
            lines = [f"№{x['number']} · {x['bug']}" + (' — текущий' if q == x else '')
                     for x in pending[:30]]
            topics = groups(self.state)
            major = sum(len(qs) for qs in topics.values())
            self.notice('Очередь подтверждений: ' + str(len(pending)) + '\n'
                        + ('\n'.join(lines) if lines else 'Простых вопросов сейчас нет.')
                        + f'\nДля обсуждения в Codex: {len(topics)} тем, {major} багов. /decisions — темы.')
            return
        if not callback and text in {'/current', 'текущий вопрос'}:
            if q:
                self.notice(f"Текущий вопрос №{q['number']} · {q['bug']}:\n"
                            + q['question'] + '\nПредлагаю: ' + q['proposal']
                            + '\nОтветь на исходное сообщение или используй его кнопки.')
            else:
                self.notice('Сейчас нет активного вопроса. Очередь: /queue.')
            return
        if not callback and text == '/next' and not q:
            queued = sorted((x for x in self.data['questions'] if x['status'] == 'queued'),
                            key=lambda x: x['order'])
            if queued:
                queued[0]['defer_until'] = 0
            else:
                self.notice('Простых вопросов в очереди нет.')
            return
        action = None
        if callback:
            parts = callback.split(':')
            if (len(parts) != 3 or parts[0] != 'qa' or not q or parts[1] != q['id']
                    or u.get('message_id') not in [r['message_id'] for r in q['receipts']]):
                self.notice('Этот вопрос уже обработан или не активен. Текущая очередь: /queue.')
                return
            action = parts[2]
        elif text in YES | NO | LATER | DISCUSS:
            action = ('yes' if text in YES else 'no' if text in NO
                      else 'later' if text in LATER else 'discuss')
            if not q or q['status'] != 'awaiting' or not q['receipts']:
                self.notice('Нет однозначного активного вопроса для этого ответа. Очередь: /queue.')
                return
            receipt = q['receipts'][-1]
            reply = u.get('reply_to_message_id')
            if reply and reply not in [r['message_id'] for r in q['receipts']]:
                self.notice('Ответ относится к другому сообщению. Используй кнопки нужного вопроса.')
                return
            if (u.get('message_id', 0) <= receipt['message_id']
                    or u.get('date', 0) < receipt['date']
                    or (not reply and u.get('date', 0) <= self.data.get('last_answer_date', 0))):
                self.notice('Ответ мог относиться к предыдущему вопросу. Ответь на сообщение текущего вопроса или нажми его кнопку.')
                return
        else:
            if text:
                self.notice('Для простого вопроса: «да», «нет», «позже» или «обсудить». '
                            'Ответь на сообщение вопроса; список: /queue. Подробные решения обсуждаем в Codex.')
            return
        if action not in {'yes', 'no', 'later', 'discuss'} or not q or q['status'] != 'awaiting':
            return
        # No source availability => no approval and no update cursor advance; retry the same update.
        if not self.fresh(q):
            self.stale(q)
            return
        self.data['active'] = None
        self.data['last_answer_date'] = max(self.data.get('last_answer_date', 0), u.get('date', 0))
        if action == 'yes':
            q.update(status='approved', answered=self.clock(), answer='yes', update_id=u['update_id'])
            self.state.setdefault('owner_clarifications', {})[q['bug']] = {
                'source_hash': q['source_hash'],
                'text': (f"Владелец подтвердил в Telegram вопрос №{q['number']}: "
                         + q['question'] + '\nСогласованное решение: ' + q['proposal']
                         + '\nЭто ограниченное уточнение существующего интерфейса/поведения; '
                         'изменение авторизации, прав доступа, источника данных или внешних интеграций '
                         'требует отдельного обсуждения, а не этого подтверждения.'
                         + '\nРеализовать только этот сценарий, выполнить целевые проверки. '
                         'Статус «ждёт ретеста» — только после подтверждённой выкладки.'),
                'question_id': q['id'], 'telegram_update_id': u['update_id']}
            self.state['holds'].pop(q['bug'], None)
            self.state['next_plan'] = 0
            self.notice(f"Решение принято: №{q['number']} · {q['bug']} возвращён в работу. "
                        'Это подтверждение сценария; исправление ещё предстоит.')
        elif action == 'later':
            self.data['order'] += 1
            q.update(status='queued', order=self.data['order'], defer_until=self.clock() + 3600)
            self.notice(f"Вопрос №{q['number']} отложен. Вернуть раньше: /next. Молчание не считается согласием.")
        else:
            q.update(status='discuss' if action == 'discuss' else 'rejected',
                     answered=self.clock(), answer=action, update_id=u['update_id'])
            self.notice(f"№{q['number']} · {q['bug']} остаётся заблокированным. "
                        + ('Обсудим решение здесь. ' if action == 'discuss' else 'Предложенный вариант отклонён. ')
                        + 'Пришли в Codex вопрос и предложенный вариант:\n'
                        + q['question'] + '\n' + q['proposal'])

    def display(self):
        q = self.current()
        if q and q['status'] == 'awaiting':
            return
        if not q:
            ready = sorted((x for x in self.data['questions'] if x['status'] == 'queued'
                            and x.get('defer_until', 0) <= self.clock()), key=lambda x: x['order'])
            if not ready:
                return
            q = ready[0]
        if q.get('retry_after', 0) > self.clock():
            return
        if not self.fresh(q):
            self.stale(q)
            return
        waiting = sum(x['status'] in PENDING and x['id'] != q['id'] for x in self.data['questions'])
        q['status'] = 'sending'
        self.data['active'] = q['id']
        self.save()
        markup = {'inline_keyboard': [
            [{'text': 'Подтверждаю', 'callback_data': 'qa:' + q['id'] + ':yes'},
             {'text': 'Нет', 'callback_data': 'qa:' + q['id'] + ':no'}],
            [{'text': 'Обсудить здесь', 'callback_data': 'qa:' + q['id'] + ':discuss'},
             {'text': 'Позже', 'callback_data': 'qa:' + q['id'] + ':later'}]]}
        result = self.transport({'action': 'send', 'text':
            f"Вопрос №{q['number']} · {q['bug']}\n" + q['question']
            + '\nПочему нужен ответ: ' + q['reason'][:600]
            + '\n\nПредлагаю: ' + q['proposal']
            + '\n\nПодтверждаешь? Можно ответить на это сообщение: «да», «подтверждаю», «нет» или «позже».'
            + f'\nЕщё вопросов в очереди: {waiting}. /queue — список.', 'reply_markup': markup})
        if result.get('ok') and result.get('message_id'):
            q['receipts'].append({'message_id': result['message_id'], 'date': result['date']})
            q['status'] = 'awaiting'
            q.pop('retry_after', None)
        else:
            q['retry_after'] = self.clock() + 60
        self.save()
