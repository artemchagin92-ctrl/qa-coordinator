"""Group unresolved architectural blockers into a single editable owner digest."""
import hashlib
import time


TITLES = {
    'automation': 'Автоматизация',
    'access': 'Права доступа',
    'migration': 'Миграция данных',
    'other': 'Другие вопросы для обсуждения',
}


def topic(question):
    reason = question['reason'].lower()
    if 'автоматизац' in reason:
        return 'automation'
    if 'доступ' in reason or 'permission' in reason:
        return 'access'
    if 'миграц' in reason or 'migration' in reason:
        return 'migration'
    return 'other'


def groups(state):
    result = {}
    for q in state.get('telegram_approvals', {}).get('questions', []):
        if (q['status'] not in {'architecture', 'discuss', 'rejected'}
                or state.get('holds', {}).get(q['bug'], {}).get('source_hash') != q['source_hash']):
            continue
        result.setdefault(topic(q), []).append(q)
    return dict(sorted(result.items(), key=lambda item: (-len(item[1]), item[0])))


def summary(state):
    grouped = groups(state)
    total = sum(len(qs) for qs in grouped.values())
    if not total:
        return 'QA: все сохранённые вопросы для обсуждения разрешены. Это не означает, что все исправления уже завершены.'
    lines = ['QA — вопросы для Codex',
             f'Для обсуждения: {len(grouped)} тем, {total} связанных багов.', '']
    for key, questions in grouped.items():
        lines.append(f'{TITLES[key]} — {len(questions)} · /topic {key}')
    lines.extend(['', 'Одна тема может разблокировать несколько багов, но не подтверждает их исправление.',
                  'Обсуждаем архитектуру в Codex. Короткое «да» эти темы не разрешает.',
                  '/decisions — текущая сводка. /queue — простые подтверждения.',
                  'Пересылать каждый баг не нужно: вопросы сохранены на QA-сервере.'])
    return '\n'.join(lines)


def details(state, key):
    questions = groups(state).get(key)
    if not questions:
        return 'Такой открытой темы нет. Список: /decisions.'
    ids = ', '.join(q['bug'] for q in questions)
    examples = '\n\n'.join(q['bug'] + ': ' + q['reason'][:500] for q in questions[:3])
    return (TITLES[key] + f' — {len(questions)} связанных багов\n' + ids
            + '\n\nПримеры препятствий:\n' + examples
            + '\n\nОбсудим эту тему в Codex. Общего подтверждения «да» здесь нет.')[:3900]


class DiscussionDigest:
    def __init__(self, state, save, transport, clock=time.time):
        self.state, self.save, self.transport, self.clock = state, save, transport, clock
        self.data = state.setdefault('telegram_approvals', {}).setdefault('discussion_digest', {})

    def publish(self):
        if not groups(self.state) and not self.data.get('message_id'):
            return
        text = summary(self.state)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.data.get('text_hash') == digest or self.data.get('retry_after', 0) > self.clock():
            return
        if self.data.get('next_edit', 0) > self.clock():
            return
        message_id = self.data.get('message_id')
        request = {'action': 'edit' if message_id else 'send', 'text': text}
        if message_id:
            request['message_id'] = message_id
        result = self.transport(request)
        if result.get('ok') and (message_id or result.get('message_id')):
            self.data.update(message_id=message_id or result['message_id'], text_hash=digest,
                             updated=self.clock(), next_edit=self.clock() + 60)
            self.data.pop('retry_after', None)
            self.data.pop('last_error', None)
        else:
            if result.get('missing_message'):
                self.data.pop('message_id', None)
            self.data.update(retry_after=self.clock() + 60,
                             last_error=result.get('error', 'digest transport failed'))
        self.save()
