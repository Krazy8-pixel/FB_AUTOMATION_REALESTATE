"""Transactional, account-scoped listing snapshots and repost preferences."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from automation.runtime_paths import data_dir


@contextmanager
def connection():
    db = sqlite3.connect(str(data_dir() / 'listings.db'), timeout=30)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS snapshots (account TEXT PRIMARY KEY, updated_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS listings (account TEXT, id TEXT, position INTEGER, data TEXT, preferences TEXT, PRIMARY KEY(account,id))')
        db.execute('CREATE TABLE IF NOT EXISTS listing_sync (account TEXT PRIMARY KEY, imported_at TEXT)')
        with db:
            yield db
    finally:
        db.close()


def load(account, legacy_path=None):
    with connection() as db:
        meta = db.execute('SELECT updated_at FROM snapshots WHERE account=?', (account,)).fetchone()
        if meta is None and legacy_path and legacy_path.exists():
            # Leave the old JSON intact as a migration backup.
            raw = json.loads(legacy_path.read_text(encoding='utf-8'))
            _save(db, account, raw.get('listings') or [], raw.get('updated_at'))
            meta = db.execute('SELECT updated_at FROM snapshots WHERE account=?', (account,)).fetchone()
        rows = db.execute('SELECT data,preferences FROM listings WHERE account=? ORDER BY position', (account,)).fetchall()
        items = []
        for data, preferences in rows:
            item = json.loads(data)
            item['repost_preferences'] = json.loads(preferences or '{}')
            items.append(item)
        imported = db.execute('SELECT imported_at FROM listing_sync WHERE account=?', (account,)).fetchone()
        return {'account_email': account, 'updated_at': meta[0] if meta else None,
                'last_import_at': imported[0] if imported else (meta[0] if meta else None), 'listings': items}


def _save(db, account, items, updated_at=None):
    preferences = dict(db.execute('SELECT id,preferences FROM listings WHERE account=?', (account,)))
    db.execute('DELETE FROM listings WHERE account=?', (account,))
    for position, source in enumerate(items):
        item = dict(source)
        supplied = item.pop('repost_preferences', {})
        listing_id = str(item['id'])
        preferred = json.loads(preferences[listing_id]) if preferences.get(listing_id) else supplied
        if 'downloaded_images' in item:
            names = [photo['name'] for photo in item['downloaded_images']]
            order = [name for name in preferred.get('image_order', []) if name in names]
            preferred['image_order'] = order + [name for name in names if name not in order]
        db.execute('INSERT INTO listings VALUES (?,?,?,?,?)',
                   (account, listing_id, position, json.dumps(item, ensure_ascii=False),
                    json.dumps(preferred, ensure_ascii=False)))
    db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',
               (account, updated_at or datetime.now(timezone.utc).isoformat(timespec='seconds')))


def save(account, items):
    with connection() as db:
        _save(db, account, items)


def add_at_top(account, source):
    """Insert one confirmed listing without replacing existing account records."""
    item = dict(source)
    preferences = item.pop('repost_preferences', {})
    with connection() as db:
        first = db.execute('SELECT MIN(position) FROM listings WHERE account=?', (account,)).fetchone()[0]
        db.execute('INSERT INTO listings VALUES (?,?,?,?,?)',
                   (account, str(item['id']), (first - 1) if first is not None else 0,
                    json.dumps(item, ensure_ascii=False), json.dumps(preferences, ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',
                   (account, datetime.now(timezone.utc).isoformat(timespec='seconds')))


def save_preferences(account, listing_id, preferences):
    with connection() as db:
        cursor = db.execute('UPDATE listings SET preferences=? WHERE account=? AND id=?',
                            (json.dumps(preferences, ensure_ascii=False), account, listing_id))
        return cursor.rowcount == 1


def update_fields(account, listing_id, changes, *, move_to_top=False):
    """Update one record without replacing another worker's account snapshot."""
    with connection() as db:
        row = db.execute('SELECT data FROM listings WHERE account=? AND id=?', (account, listing_id)).fetchone()
        if not row:
            return False
        item = json.loads(row[0])
        item.update(changes)
        db.execute('UPDATE listings SET data=? WHERE account=? AND id=?',
                   (json.dumps(item, ensure_ascii=False), account, listing_id))
        if move_to_top:
            first = db.execute('SELECT MIN(position) FROM listings WHERE account=?', (account,)).fetchone()[0]
            db.execute('UPDATE listings SET position=? WHERE account=? AND id=?',
                       (first - 1, account, listing_id))
        return True


def remove(account, listing_ids):
    """Remove only explicitly selected backup rows for this account."""
    with connection() as db:
        removed = 0
        for listing_id in set(listing_ids):
            cursor = db.execute('DELETE FROM listings WHERE account=? AND id=?', (account, str(listing_id)))
            removed += cursor.rowcount
        return removed


def mark_import_complete(account):
    # Click refreshes and local deletes must not postpone the daily import.
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO listing_sync VALUES (?,?)',
                   (account, datetime.now(timezone.utc).isoformat(timespec='seconds')))
