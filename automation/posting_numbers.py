"""Read a numeric amount without treating punctuation in currency labels as digits."""
from decimal import Decimal, InvalidOperation
import math
import re
import unicodedata


_DIRECTION_MARKS = dict.fromkeys(map(ord, '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'))


def _ascii_number_text(value):
    text = unicodedata.normalize('NFKC', str(value))
    converted = []
    for char in text:
        try:
            converted.append(str(unicodedata.decimal(char)))
        except (ValueError, TypeError):
            converted.append(char)
    return ''.join(converted).translate(_DIRECTION_MARKS).replace('\u066c', ',').replace('\u066b', '.').replace('\u2212', '-').replace('\u2019', "'")


def _integer_digits(text, allowed_grouping):
    """Validate grouping before removing it; never join arbitrary text/numbers."""
    if not text:
        return '0'
    if text.isascii() and text.isdigit():
        return text
    separators = {char for char in text if not char.isdigit()}
    if len(separators) != 1 or not separators.issubset(allowed_grouping):
        return None
    separator = separators.pop()
    groups = text.split(separator)
    if (not groups or not 1 <= len(groups[0]) <= 3 or
            any(not group.isascii() or not group.isdigit() for group in groups) or
            any(len(group) != 3 for group in groups[1:])):
        return None
    return ''.join(groups)


def parse_numeric_value(value):
    """Return the amount as Decimal, or None for missing/ambiguous numeric text.

    Currency text and punctuation *outside* the first-to-last digit span are
    ignored. Inside that span, only validated grouping/decimal separators are
    accepted, so '100 USD 200' cannot silently become 100200.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    text = ' '.join(_ascii_number_text(value).split())
    positions = [match.start() for match in re.finditer(r'[0-9]', text)]
    if not positions:
        return None
    first, last = positions[0], positions[-1]
    prefix, body, suffix = text[:first], text[first:last + 1], text[last + 1:]
    if not re.fullmatch(r"[0-9., '\u0027]+", body):
        return None

    negative = False
    prefix_signs = [char for char in prefix if char in '+-']
    if prefix_signs:
        # Support -$15 and USD -15 while refusing mixed/repeated signs.
        if len(prefix_signs) != 1 or not (prefix.strip().startswith(prefix_signs[0]) or prefix.rstrip().endswith(prefix_signs[0])):
            return None
        negative = prefix_signs[0] == '-'
    if any(char in '+-' for char in suffix):
        return None
    if '(' in prefix or ')' in suffix:
        if prefix.count('(') != 1 or suffix.count(')') != 1 or prefix_signs:
            return None
        negative = True

    # Leading .50 is a decimal, but the dot in Rs.50 is currency punctuation.
    if first and text[first - 1] in '.,' and body.isdigit():
        before_dot = text[:first - 1]
        if not before_dot or before_dot[-1].isspace() or unicodedata.category(before_dot[-1]) == 'Sc':
            body = '0' + text[first - 1] + body

    commas, dots = body.count(','), body.count('.')
    fraction = None
    if commas and dots:
        decimal_separator = ',' if body.rfind(',') > body.rfind('.') else '.'
        grouping_separator = '.' if decimal_separator == ',' else ','
        integer, fraction = body.rsplit(decimal_separator, 1)
        if decimal_separator in integer or not fraction.isascii() or not fraction.isdigit():
            return None
        digits = _integer_digits(integer, {grouping_separator, ' ', "'"})
    elif commas or dots:
        separator = ',' if commas else '.'
        count = commas or dots
        if count > 1:
            # Repeated grouping is valid only in complete groups of three.
            digits = _integer_digits(body, {separator})
        else:
            integer, after = body.split(separator)
            comma_grouping = separator == ',' and len(after) == 3 and integer.isdigit() and 1 <= len(integer) <= 3
            if comma_grouping:
                digits = _integer_digits(body, {','})
            else:
                # A single period is kept as a decimal, including 12.345.
                # Comma decimals with one/two/etc. fraction digits are kept.
                digits = _integer_digits(integer, {' ', "'"})
                fraction = after
                if not fraction.isascii() or not fraction.isdigit():
                    return None
    else:
        digits = _integer_digits(body, {' ', "'"})
    if digits is None:
        return None
    canonical = digits + ('.' + fraction if fraction is not None else '')
    try:
        amount = Decimal(('-' if negative else '') + canonical)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def numeric_input_value(value):
    """Return only the numeric text to enter, preserving any decimal fraction."""
    amount = parse_numeric_value(value)
    if amount is None:
        raise ValueError(f'Cannot determine one unambiguous numeric amount from {value!r}.')
    if amount == 0:
        return '0'
    text = format(amount, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text
