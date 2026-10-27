"""Verify literal customer-provided coupon identifiers, without interpreting chat."""


def _code(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 50 or any(
            not (char.isascii() and (char.isalnum() or char in '_-')) for char in value):
        return None
    return value.lower()


def _contains_code(text, code):
    if not isinstance(text, str):
        return False
    text = text.lower()
    offset = 0
    while True:
        position = text.find(code, offset)
        if position < 0:
            return False
        end = position + len(code)
        before = text[position - 1] if position else ''
        after = text[end] if end < len(text) else ''
        if not any(char.isalnum() or char in '_-' for char in (before, after) if char):
            return True
        offset = position + 1


def customer_supplied_coupon(code, message, history=(), saved=()):
    """Check the proposed identifier's provenance, not permission to apply it.

    The model chooses validate/apply and understands the customer's instructions.
    This check only prevents invented or assistant-suggested codes becoming customer
    evidence. It never classifies requests, confirmations, negations or order targets.
    """
    code = _code(code)
    if code is None:
        return False
    if isinstance(saved, (list, tuple, set)) and any(_code(value) == code for value in saved):
        return True
    if _contains_code(message, code):
        return True
    return isinstance(history, (list, tuple)) and any(
        isinstance(entry, dict) and entry.get('role') == 'user'
        and _contains_code(entry.get('content'), code) for entry in history)
