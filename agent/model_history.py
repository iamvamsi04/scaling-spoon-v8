"""A small recent transcript without cutting proposals or their human replies."""


def bounded_history(history, *, max_bytes=8000, max_turns=4):
    """Return a contiguous suffix of whole conversational turns.

    The latest turn is kept verbatim even when it exceeds the budget. In staff
    mode it may be the only record of the exact refund or other action awaiting
    approval. This budget controls old conversation text, never current tool
    results, the current request, or the customer's trusted shopping state.

    A byte budget avoids a tokenizer dependency or runtime downloads. It bounds
    English transcript size conservatively; provider token usage remains the
    authoritative measure of the complete request.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError('max_bytes must be a positive integer.')
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise ValueError('max_turns must be a positive integer.')

    turns = []
    current = []
    answered = False
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role, content = entry.get('role'), entry.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        if role == 'user' and answered:
            turns.append(current)
            current, answered = [], False
        current.append({'role': role, 'content': content})
        answered = answered or role == 'assistant'
    if current:
        turns.append(current)
    if not turns:
        return []

    def size(turn):
        # Include a small role/framing allowance as well as all UTF-8 text.
        return sum(len(message['content'].encode('utf-8')) + 32 for message in turn)

    selected = [turns[-1]]
    used = size(turns[-1])
    for turn in reversed(turns[:-1]):
        if len(selected) >= max_turns or used + size(turn) > max_bytes:
            break
        selected.append(turn)
        used += size(turn)
    return [message for turn in reversed(selected) for message in turn]
