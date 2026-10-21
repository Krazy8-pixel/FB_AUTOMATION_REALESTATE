"""Account-scoped Facebook snapshots and a shared collection of saved backups."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from automation.runtime_paths import data_dir
from automation.responsiveness import in_request


SHARED_BACKUPS = '__shared_listing_backups__'


def is_backup(item):
    return item.get('facebook_present') is False or (item.get('source_deleted') and not item.get('facebook_reposted'))


def _owner(db, account, listing_id):
    row = db.execute('SELECT account FROM listings WHERE id=? AND account IN (?,?) ORDER BY account=? DESC LIMIT 1',
                     (str(listing_id), account, SHARED_BACKUPS, account)).fetchone()
    return row[0] if row else account


@contextmanager
def connection():
    db = sqlite3.connect(str(data_dir() / 'listings.db'), timeout=1 if in_request() else 30)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS snapshots (account TEXT PRIMARY KEY, updated_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS listings (account TEXT, id TEXT, position INTEGER, data TEXT, preferences TEXT, PRIMARY KEY(account,id))')
        db.execute('CREATE TABLE IF NOT EXISTS listing_sync (account TEXT PRIMARY KEY, imported_at TEXT)')
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS listing_migrations (name TEXT PRIMARY KEY)')
            if not db.execute('SELECT 1 FROM listing_migrations WHERE name=?', ('shared_backups',)).fetchone():
                for owner, listing_id, position, raw, prefs in db.execute('SELECT account,id,position,data,preferences FROM listings WHERE account!=?', (SHARED_BACKUPS,)).fetchall():
                    item = json.loads(raw)
                    if is_backup(item):
                        item.setdefault('backup_source_account', owner)
                        db.execute('INSERT OR IGNORE INTO listings VALUES (?,?,?,?,?)',
                                   (SHARED_BACKUPS, listing_id, position, json.dumps(item, ensure_ascii=False), prefs))
                        db.execute('DELETE FROM listings WHERE account=? AND id=?', (owner, listing_id))
                db.execute('INSERT INTO listing_migrations VALUES (?)', ('shared_backups',))
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
        rows = db.execute('SELECT data,preferences FROM listings WHERE account IN (?,?) ORDER BY account=?,position', (account, SHARED_BACKUPS, SHARED_BACKUPS)).fetchall()
        items = []
        for data, preferences in rows:
            item = json.loads(data)
            item['repost_preferences'] = json.loads(preferences or '{}')
            items.append(item)
        imported = db.execute('SELECT imported_at FROM listing_sync WHERE account=?', (account,)).fetchone()
        return {'account_email': account, 'updated_at': meta[0] if meta else None,
                'last_import_at': imported[0] if imported else (meta[0] if meta else None), 'listings': items}


def _save(db, account, items, updated_at=None):
    preferences = dict(db.execute('SELECT id,preferences FROM listings WHERE account IN (?,?) ORDER BY account=?',
                                  (SHARED_BACKUPS, account, account)))
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
        owner = SHARED_BACKUPS if is_backup(item) else account
        if owner == SHARED_BACKUPS:
            item.setdefault('backup_source_account', account)
        # Imports contain shared rows too; never overwrite another operation's backup.
        db.execute('INSERT OR IGNORE INTO listings VALUES (?,?,?,?,?)',
                   (owner, listing_id, position, json.dumps(item, ensure_ascii=False),
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
    if is_backup(item):
        item.setdefault('backup_source_account', account)
        account = SHARED_BACKUPS
    with connection() as db:
        first = db.execute('SELECT MIN(position) FROM listings WHERE account=?', (account,)).fetchone()[0]
        db.execute('INSERT INTO listings VALUES (?,?,?,?,?)',
                   (account, str(item['id']), (first - 1) if first is not None else 0,
                    json.dumps(item, ensure_ascii=False), json.dumps(preferences, ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',
                   (account, datetime.now(timezone.utc).isoformat(timespec='seconds')))


def save_preferences(account, listing_id, preferences):
    with connection() as db:
        account = _owner(db, account, listing_id)
        cursor = db.execute('UPDATE listings SET preferences=? WHERE account=? AND id=?',
                            (json.dumps(preferences, ensure_ascii=False), account, listing_id))
        return cursor.rowcount == 1


def update_fields(account, listing_id, changes, *, move_to_top=False):
    """Update one record without replacing another worker's account snapshot."""
    with connection() as db:
        owner = _owner(db, account, listing_id)
        row = db.execute('SELECT data,preferences,position FROM listings WHERE account=? AND id=?', (owner, listing_id)).fetchone()
        if not row:
            return False
        item = json.loads(row[0])
        item.update(changes)
        target = SHARED_BACKUPS if is_backup(item) else account
        if target == SHARED_BACKUPS:
            item.setdefault('backup_source_account', account)
        position = row[2]
        if move_to_top or target != owner:
            first = db.execute('SELECT MIN(position) FROM listings WHERE account=?', (target,)).fetchone()[0]
            position = first - 1 if first is not None else 0
        db.execute('DELETE FROM listings WHERE account=? AND id=?', (owner, listing_id))
        db.execute('INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?)',
                   (target, listing_id, position, json.dumps(item, ensure_ascii=False), row[1]))
        return True


def remove(account, listing_ids):
    """Remove selected account rows or shared backups after confirmed deletion."""
    with connection() as db:
        removed = 0
        for listing_id in set(listing_ids):
            owner = _owner(db, account, listing_id)
            cursor = db.execute('DELETE FROM listings WHERE account=? AND id=?', (owner, str(listing_id)))
            removed += cursor.rowcount
        return removed


def clear_facebook(account):
    """Remove only this account's local Facebook records, keeping shared backups."""
    with connection() as db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        return db.execute('DELETE FROM listings WHERE account=?', (account,)).rowcount


def clear_backups(account):
    """Clear the shared backup collection without removing account Facebook rows."""
    with connection() as db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        removed = 0
        for listing_id, raw in db.execute('SELECT id,data FROM listings WHERE account=?', (SHARED_BACKUPS,)).fetchall():
            item = json.loads(raw)
            if item.get('facebook_present') is False or (item.get('source_deleted') and not item.get('facebook_reposted')):
                removed += db.execute('DELETE FROM listings WHERE account=? AND id=?', (SHARED_BACKUPS, listing_id)).rowcount
        return removed


def remove_backup(account, listing_id):
    """Remove one backup only if it has not become a live listing meanwhile."""
    with connection() as db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        account = SHARED_BACKUPS
        row = db.execute('SELECT data FROM listings WHERE account=? AND id=?', (account, listing_id)).fetchone()
        if not row:
            return False
        item = json.loads(row[0])
        if item.get('facebook_present') is not False and not (item.get('source_deleted') and not item.get('facebook_reposted')):
            return False
        from automation.backup_duplicates import content_key
        item_key = content_key(dict(item, repost_preferences=json.loads(db.execute(
            'SELECT preferences FROM listings WHERE account=? AND id=?', (account, listing_id)).fetchone()[0] or '{}')))
        ids = [listing_id]
        if item_key:
            for other_id, raw, preferences in db.execute('SELECT id,data,preferences FROM listings WHERE account=? AND id!=?', (account, listing_id)).fetchall():
                candidate = dict(json.loads(raw), repost_preferences=json.loads(preferences or '{}'))
                if content_key(candidate) == item_key:
                    ids.append(other_id)
        db.executemany('DELETE FROM listings WHERE account=? AND id=?', [(account, value) for value in ids])
        return True


def mark_import_complete(account):
    # Click refreshes and local deletes must not postpone the daily import.
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO listing_sync VALUES (?,?)',
                   (account, datetime.now(timezone.utc).isoformat(timespec='seconds')))
