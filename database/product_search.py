"""Product text matching without changing identifiers, prices or stored labels."""
import re
import unicodedata


def _word(word):
    if word == 'mice':
        return 'mouse'
    if word in {'series', 'species', 'lens', 'gas', 'bus'}:
        return word
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 4 and word.endswith(('ches', 'shes', 'xes', 'zes', 'sses')):
        return word[:-2]
    if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def product_text(value):
    """Fold case/separators and common plurals for human product labels only."""
    if value is None:
        return None
    words = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value)).casefold())
    return ' '.join(_word(word) for word in words)


def product_matches(value, search):
    """Match a complete normalized word or phrase, never a partial identifier."""
    value, search = product_text(value), product_text(search)
    return int(bool(value and search and f' {search} ' in f' {value} '))


def product_like(value, pattern):
    """LIKE on normalized product labels; percent remains a wildcard."""
    if value is None or pattern is None:
        return None
    # Underscores in product labels mean spaces, so only percent is a wildcard.
    parts = []
    for part in str(pattern).split('%'):
        normalized = product_text(part) or ''
        if normalized and re.match(r'[\s_\-]', part):
            normalized = ' ' + normalized
        if normalized and re.search(r'[\s_\-]$', part):
            normalized += ' '
        parts.append(normalized)
    value = product_text(value) or ''
    if len(parts) == 1:
        return int(value == parts[0])
    if not value.startswith(parts[0]):
        return 0
    position = len(parts[0])
    for part in parts[1:-1]:
        found = value.find(part, position)
        if found == -1:
            return 0
        position = found + len(part)
    return int(value.endswith(parts[-1]) and len(value) - len(parts[-1]) >= position)


def register_product_search(conn):
    conn.create_function('product_text', 1, product_text, deterministic=True)
    conn.create_function('product_matches', 2, product_matches, deterministic=True)
    conn.create_function('product_like', 2, product_like, deterministic=True)


_TOKEN = re.compile(r"\s+|--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\?[0-9]*|[A-Za-z_][\w]*|==|!=|<>|[^\s]", re.UNICODE)
_KEYWORDS = {'WHERE', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'FULL',
             'ON', 'USING', 'GROUP', 'ORDER', 'LIMIT', 'OFFSET', 'HAVING', 'UNION',
             'INDEXED', 'NOT', 'AS', 'SELECT', 'FROM', 'WINDOW'}


def normalize_product_filters(query):
    """Repair direct product name/category filters in a single SELECT.

    Only proven table columns and bound/string values are rewritten. Nested SQL,
    expression filters, explicit collations and escaped LIKE patterns retain
    their original semantics; product_matches is available for those queries.
    Comments and string literals are tokenized rather than searched as SQL.
    """
    tokens = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(query)
              if not m.group().isspace() and not m.group().startswith(('--', '/*'))]
    if sum(t[0].upper() == 'SELECT' for t in tokens) != 1 or any(t[0].upper() == 'WITH' for t in tokens):
        return query
    where = next((i for i, t in enumerate(tokens) if t[0].upper() == 'WHERE'), None)
    source = next((i for i, t in enumerate(tokens) if t[0].upper() == 'FROM'), None)
    if where is None or source is None or any(t[0] == ',' for t in tokens[source:where]):
        return query

    def identifier(token):
        raw = token[0]
        if raw[:1] in {'"', '`', '['}:
            return raw[1:-1].replace(raw[:1] * 2, raw[:1]).casefold()
        return raw.casefold() if re.fullmatch(r'[A-Za-z_][\w]*', raw) else None

    aliases, tables = {}, []
    for index, token in enumerate(tokens):
        if token[0].upper() not in {'FROM', 'JOIN'} or index + 1 >= len(tokens):
            continue
        end = index + 1
        table = identifier(tokens[end])
        if end + 2 < len(tokens) and tokens[end + 1][0] == '.':
            if table != 'main':
                return query
            end += 2
            table = identifier(tokens[end])
        if table is None:
            return query
        tables.append(table)
        alias = table
        if end + 2 < len(tokens) and tokens[end + 1][0].upper() == 'AS':
            alias = identifier(tokens[end + 2])
        elif end + 1 < len(tokens):
            possible = identifier(tokens[end + 1])
            if possible and tokens[end + 1][0].upper() not in _KEYWORDS:
                alias = possible
        if alias in aliases:
            return query
        aliases[alias] = table

    def field(index):
        if index >= len(tokens):
            return None
        name = identifier(tokens[index])
        end = index + 1
        if end + 1 < len(tokens) and tokens[end][0] == '.':
            table = aliases.get(name)
            name = identifier(tokens[end + 1])
            end += 2
        else:
            table = tables[0] if len(tables) == 1 else None
        if (table, name) not in {('items', 'name'), ('product_details', 'category')}:
            return None
        return end

    def operand(index, product=False):
        if index >= len(tokens):
            return None
        start = index
        wrappers = 0
        while (index + 1 < len(tokens) and tokens[index][0].upper() in {'LOWER', 'UPPER', 'TRIM'}
               and tokens[index + 1][0] == '('):
            wrappers += 1
            index += 2
        if product:
            end = field(index)
        else:
            raw = tokens[index][0] if index < len(tokens) else ''
            end = index + 1 if raw.startswith(("'", '?')) else None
        if end is None:
            return None
        for _ in range(wrappers):
            if end >= len(tokens) or tokens[end][0] != ')':
                return None
            end += 1
        return end, query[tokens[index][1]:tokens[end - wrappers - 1][2]], start

    replacements = []
    for index, token in enumerate(tokens):
        if index <= where or tokens[index - 1][0].upper() not in {'WHERE', 'AND', 'OR', '('}:
            continue
        left = operand(index, product=True)
        if left is None or left[0] >= len(tokens) or tokens[left[0]][0].upper() not in {'=', '==', 'LIKE', 'IN'}:
            continue
        operator = tokens[left[0]][0].upper()
        if operator == 'IN':
            end = left[0] + 1
            if end >= len(tokens) or tokens[end][0] != '(':
                continue
            end += 1
            values = []
            while end < len(tokens):
                right = operand(end)
                if right is None:
                    values = []
                    break
                values.append(f'product_text({right[1]})')
                end = right[0]
                if end >= len(tokens) or tokens[end][0] != ',':
                    break
                end += 1
            if not values or end >= len(tokens) or tokens[end][0] != ')':
                continue
            end += 1
            replacement = f'product_text({left[1]}) IN ({", ".join(values)})'
        else:
            right = operand(left[0] + 1)
            if right is None:
                continue
            end = right[0]
            replacement = (f'product_like({left[1]}, {right[1]})' if operator == 'LIKE' else
                           f'product_text({left[1]}) = product_text({right[1]})')
        if end < len(tokens) and tokens[end][0].upper() not in {')', 'AND', 'OR', 'GROUP', 'ORDER', 'LIMIT', 'HAVING', 'OFFSET', ';'}:
            continue
        replacements.append((token[1], tokens[end - 1][2], replacement))
    for start, end, replacement in reversed(replacements):
        query = query[:start] + replacement + query[end:]
    return query
