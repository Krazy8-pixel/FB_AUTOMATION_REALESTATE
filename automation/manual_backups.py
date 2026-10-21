"""Durable account-local backups of manual templates and confirmed posts."""
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image
from automation import listing_store
from automation.runtime_paths import data_dir


def save_manual_listing(account, payload, *, confirmed=False):
    listing_id = payload['_manual_record_id']
    root = data_dir('manual_backups') / hashlib.sha256(account.encode()).hexdigest()[:24] / listing_id
    root.mkdir(parents=True, exist_ok=True)
    photos = []
    for index in range(1, 21):
        source = payload.get(f'image_path{index}')
        if not source:
            continue
        source = Path(source)
        with Image.open(source) as image:
            width, height = image.size
            image.verify()
        name = hashlib.sha256(source.read_bytes()).hexdigest()[:24] + source.suffix.lower()
        destination = root / name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        if not any(photo['name'] == name for photo in photos):
            photos.append({'name': name, 'path': str(destination.resolve()), 'width': width, 'height': height})
    if not photos:
        raise ValueError('At least one valid photo is required.')
    videos = []
    for source in (payload.get('video_paths') or [])[:5]:
        try:
            source = Path(source)
            destination = root / ('video_' + source.name)
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            videos.append({'name': destination.name, 'path': str(destination.resolve())})
        except OSError:
            continue
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    details = {key: value for key, value in payload.items()
               if not key.startswith('_') and not key.startswith('image_path') and key not in {'group_ids', 'group_names'}}
    details['image_urls'] = []
    details['video_paths'] = [video['path'] for video in videos]
    record = {'id': listing_id, 'details': details, 'downloaded_images': photos, 'downloaded_videos': videos,
              'image_errors': [], 'video_errors': [], 'detail_error': '', 'created_at': now,
              'details_imported_at': now, 'url': payload.get('_confirmed_listing_url', '') if confirmed else '', 'edit_url': '', 'clicks': 0, 'clicks_confirmed': confirmed,
              'source_deleted': not confirmed, 'facebook_present': confirmed, 'facebook_reposted': False,
              'manual_template': not confirmed, 'listed_date': datetime.now().date().isoformat() if confirmed else '',
              'facebook_posted_at': now if confirmed else '',
              **{key: details.get(key, '') for key in ('title', 'year', 'make', 'model', 'price')}}
    if not listing_store.update_fields(account, listing_id, record, move_to_top=True):
        listing_store.add_at_top(account, record)
    return listing_id
