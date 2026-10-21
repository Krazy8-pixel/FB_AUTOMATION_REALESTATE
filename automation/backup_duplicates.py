"""Group shared backups by year, make, model, price, and mileage only."""
import re
from decimal import Decimal, InvalidOperation

FIELDS = ('year', 'make', 'model', 'price', 'mileage')


def is_backup(item):
    return item.get('facebook_present') is False or (item.get('source_deleted') and not item.get('facebook_reposted'))


def content_key(item):
    if not is_backup(item):
        return None
    details = dict(item.get('details') or {})
    details.update((item.get('repost_preferences') or {}).get('fields') or {})
    values = []
    for field in FIELDS:
        value = details.get(field, item.get(field))
        value = re.sub(r'\s+', ' ', '' if value is None else str(value)).strip().casefold()
        # Incomplete listings cannot establish that all five fields match.
        if not value:
            return None
        if field in {'year', 'price', 'mileage'}:
            try:
                number = Decimal(value.replace(',', '').removeprefix('$').strip())
                if number.is_finite():
                    value = format(number.normalize(), 'f')
            except InvalidOperation:
                pass
        values.append(value)
    return tuple(values)


def backup_groups(items):
    groups = {}
    for item in items:
        if is_backup(item):
            key = content_key(item) or ('id', str(item['id']))
            groups.setdefault(key, []).append(item)
    return list(groups.values())


def unique_listings(items):
    seen = set()
    result = []
    for item in items:
        key = content_key(item)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        result.append(item)
    return result
