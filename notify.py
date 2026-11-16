"""Filter Telegram owner messages for the approval queue."""


def owner_update(update, owner):
    """Discard other users' content while still advancing the transport cursor."""
    out = {'update_id': update['update_id'], 'authorized': False}
    callback = update.get('callback_query')
    msg = callback.get('message', {}) if callback else update.get('message', {})
    sender = callback.get('from', {}) if callback else msg.get('from', {})
    chat = msg.get('chat', {})
    if sender.get('id') != owner or chat.get('id') != owner or chat.get('type') != 'private':
        return out
    out.update(authorized=True, message_id=msg.get('message_id'), date=msg.get('date', 0))
    if callback:
        out.update(callback_id=callback['id'], callback_data=callback.get('data', ''))
    else:
        out.update(text=msg.get('text', ''),
                   reply_to_message_id=msg.get('reply_to_message', {}).get('message_id'))
    return out
