"""Plain customer-facing text; structured tool and memory payloads stay internal."""
import json
import re

FALLBACK = 'I could not display that answer clearly. Please ask me to check the current records before repeating an order or refund request.'


def reply_envelope(text):
    """Find a complete internal envelope even when the model adds surrounding prose.

    Decode whole JSON objects so escaped quotes/braces and nested memory are safe.
    Never concatenate the prose and reply: the envelope is the canonical answer.
    """
    decoder = json.JSONDecoder()
    offset = 0
    envelope = None
    while True:
        start = text.find('{', offset)
        if start < 0:
            return envelope
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            offset = start + 1
            continue
        offset = end
        if isinstance(value, dict) and (
                isinstance(value.get('reply'), str) or isinstance(value.get('response'), str)):
            envelope = value


def plain_reply(content):
    text = str(content).strip()
    envelope = reply_envelope(text)
    if envelope is not None:
        visible = envelope.get('reply', envelope.get('response'))
        return plain_reply(visible) if isinstance(visible, str) and visible.strip() and visible != text else FALLBACK
    # A truncated appended envelope must not leak internal fields onto the screen.
    fragment = re.search(r'\{\s*"(?:reply|response|shopping_update)"\s*:', text)
    if fragment:
        text = re.sub(r'```(?:json)?\s*$', '', text[:fragment.start()]).strip()
        if not text:
            return FALLBACK
    text = re.sub(r'^```(?:json)?\s*\n?(.*?)\n?```$', r'\1', text, flags=re.S)
    if text.startswith(('{', '[')):
        try:
            payload = json.loads(text)
        except ValueError:
            if text.startswith('{') or re.match(r'^\[\s*[\{"\[]', text):
                return FALLBACK
        else:
            if not isinstance(payload, dict):
                return FALLBACK
            reply = payload.get('reply', payload.get('response'))
            if not isinstance(reply, str) or not reply.strip() or reply == text:
                return FALLBACK
            return plain_reply(reply)
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'</?(?:p|br|div|span|b|strong|i|em|code|pre|ul|ol|li|h[1-6])(?:\s+[^>]*)?\s*/?>', '', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|>\s*)', '', text)
    text = re.sub(r'(\*\*|__|~~)(.+?)\1', r'\2', text, flags=re.S)
    text = re.sub(r'(?<!\w)([*_])([^\n]+?)\1(?!\w)', r'\2', text)
    text = text.replace('`', '')
    # Convert Markdown tables to labelled prose without losing column associations.
    lines, table = [], []
    def flush_table():
        if not table:
            return
        headers, *rows = table
        if not rows:
            lines.append('; '.join(headers))
        for cells in rows:
            lines.append('; '.join(f'{headers[i] if i < len(headers) else "Value"}: {c}' for i, c in enumerate(cells)) + '.')
        table.clear()
    for line in text.splitlines():
        if '|' in line:
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if not all(re.fullmatch(r':?-+:?', c) for c in cells):
                table.append(cells)
        else:
            flush_table()
            lines.append(line)
    flush_table()
    text = '\n'.join(lines)
    for symbol, word in {'$':' dollars ', '₹':' INR ', '€':' EUR ', '£':' GBP ', '%':' percent',
                         '→':' to ', '≥':' at least ', '≤':' at most ', '×':' times ',
                         '°':' degrees ', '–':'-', '—':'. '}.items():
        text = text.replace(symbol, word)
    text = ''.join(c for c in text if not (0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF) and c not in '\u200d\ufe0f\u20e3')
    text = re.sub(r'(?m)^[ \t]*[-*_]{3,}[ \t]*$', '', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    return re.sub(r'\n{3,}', '\n\n', text).strip() or FALLBACK


def error_reply(response):
    """Extract the safe message, never display a raw HTTP JSON body."""
    try:
        detail = response.json().get('detail')
        if isinstance(detail, dict):
            detail = detail.get('message')
        if isinstance(detail, str):
            return plain_reply(detail)
    except (ValueError, AttributeError):
        pass
    return 'The request could not be completed. Check your connection and request details, then try again.'


def _markdown_cells(line):
    """Read literal Markdown cells; an escaped pipe stays in its own cell."""
    value = line.strip()
    cells, cell, escaped = [], [], False
    separators = 0
    for char in value:
        if char == '|' and not escaped:
            cells.append(''.join(cell).strip())
            cell = []
            separators += 1
        elif char == '|' and escaped:
            cell.pop()  # The single escape before this pipe is presentation syntax.
            cell.append(char)
        else:
            cell.append(char)
        escaped = not escaped if char == '\\' else False
    cells.append(''.join(cell).strip())
    if not separators:
        return None
    if value.startswith('|'):
        cells.pop(0)
    # A final escaped pipe belongs to the last cell, not the table border.
    trailing_slashes = len(value[:-1]) - len(value[:-1].rstrip('\\')) if value.endswith('|') else 0
    if value.endswith('|') and trailing_slashes % 2 == 0:
        cells.pop()
    return cells


def _explanation_table_title(lines, index):
    for line in reversed(lines[:index]):
        if not line.strip():
            continue
        candidate = re.sub(r'^\s*#{1,6}\s+', '', line).strip()
        candidate = re.sub(r'^(\*\*|__)(.+)\1$', r'\2', candidate).rstrip(':').strip()
        if (candidate and len(candidate) <= 120 and len(candidate.split()) <= 16
                and '|' not in candidate and not candidate.endswith(('.', '?', '!'))):
            return candidate
        break
    return 'Explanation'


def split_reply_tables(content):
    """Extract bounded explanation tables, retaining the normal reply transport.

    The caller must restrict this to general explanations without successful
    business-tool receipts. Current business facts require verified tool tables.
    Malformed or over-limit tables remain untouched for ``plain_reply`` to handle.
    This function does not interpret numbers, evaluate cells or change memory.
    """
    from context.tables import MAX_TABLES, MAX_TOTAL_BYTES, table_artifact

    if not isinstance(content, str):
        return content, []
    envelope = reply_envelope(content)
    visible = envelope.get('reply', envelope.get('response')) if envelope is not None else content
    if not isinstance(visible, str):
        return content, []
    lines = visible.splitlines()
    kept, tables, total_bytes, index, fence = [], [], 0, 0, None
    while index < len(lines):
        line = lines[index]
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            kept.append(line)
            index += 1
            continue
        headers = _markdown_cells(line) if fence is None else None
        separator = _markdown_cells(lines[index + 1]) if headers and index + 1 < len(lines) else None
        if not (headers and separator and len(headers) == len(separator)
                and all(re.fullmatch(r':?-{3,}:?', cell) for cell in separator)):
            kept.append(line)
            index += 1
            continue
        end, rows, malformed = index + 2, [], False
        while end < len(lines):
            row = _markdown_cells(lines[end])
            if row is None or re.match(r'^\s*(`{3,}|~{3,})', lines[end]):
                break
            if len(row) != len(headers) or all(re.fullmatch(r':?-{3,}:?', cell) for cell in row):
                malformed = True
            rows.append(row)
            end += 1
        if not malformed and len(tables) < MAX_TABLES:
            try:
                table = table_artifact(_explanation_table_title(lines, index), headers, rows,
                                       coverage='General explanation.')
                size = len(json.dumps(table, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                if total_bytes + size > MAX_TOTAL_BYTES:
                    raise ValueError('Explanation tables exceed the safe attachment size.')
            except (ValueError, TypeError, OverflowError):
                pass
            else:
                tables.append(table)
                total_bytes += size
                index = end
                continue
        kept.extend(lines[index:end])
        index = end
    if not tables:
        return content, []
    cleaned = re.sub(r'\n{3,}', '\n\n', '\n'.join(kept)).strip()
    cleaned = cleaned or 'The explanation is shown in the table.'
    if envelope is None:
        return cleaned, tables
    updated = dict(envelope)
    updated['reply' if 'reply' in updated else 'response'] = cleaned
    return json.dumps(updated, ensure_ascii=False), tables
