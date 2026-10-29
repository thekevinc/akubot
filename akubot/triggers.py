"""Recognize direct replies from supplied metadata, without fetching history."""

def is_reply_to_bot(message, bot_id: int) -> bool:
    reference = getattr(message, 'reference', None)
    guild_id = getattr(getattr(message, 'guild', None), 'id', None)
    channel_id = getattr(getattr(message, 'channel', None), 'id', None)
    if reference is None or guild_id is None or channel_id is None:
        return False
    kind = getattr(reference, 'type', 0)
    if getattr(kind, 'value', kind) != 0:
        return False  # Forwarded messages are not direct replies.
    if getattr(reference, 'guild_id', guild_id) not in (None, guild_id):
        return False
    if getattr(reference, 'channel_id', None) != channel_id:
        return False
    message_id = getattr(reference, 'message_id', None)
    if type(message_id) is not int or message_id <= 0:
        return False
    parent = getattr(reference, 'resolved', None)
    if parent is None or getattr(parent, 'id', None) != getattr(reference, 'message_id', None):
        return False
    if getattr(getattr(parent, 'author', None), 'id', None) != bot_id:
        return False
    if getattr(parent, 'webhook_id', None) is not None:
        return False
    return (
        getattr(getattr(parent, 'guild', None), 'id', None) == guild_id
        and getattr(getattr(parent, 'channel', None), 'id', None) == channel_id
    )
