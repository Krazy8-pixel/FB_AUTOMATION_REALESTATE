"""Listing video discovery and bounded, original-byte backups when present."""
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


def allowed_source(url):
    parsed = urlsplit(str(url))
    return parsed.scheme == 'https' and (parsed.hostname or '').endswith('.fbcdn.net')


def discover(driver):
    try:
        sources = driver.execute_script("""
            return [...document.querySelectorAll('video')].flatMap(video =>
                [video.currentSrc, video.getAttribute('src'),
                 ...[...video.querySelectorAll('source')].map(source => source.src)]
            ).filter(Boolean);
        """) or []
        direct, errors = [], []
        for url in dict.fromkeys(sources):
            if allowed_source(url):
                direct.append(url)
            else:
                errors.append('Video uses a blob, streaming, or unsupported URL; no downloadable original recovered.')
        return direct, errors
    except Exception as exc:
        return [], [f'Optional video detection failed: {exc}']


def saved_paths(listing):
    return [item['path'] for item in listing.get('downloaded_videos', [])
            if item.get('path') and os.path.isfile(item['path'])]


def download(driver, account, listing_id, urls, root, stop_event):
    videos, errors = [], []
    if not urls:
        return videos, errors
    try:
        folder = Path(root) / 'videos' / hashlib.sha256(account.encode()).hexdigest()[:24] / hashlib.sha256(str(listing_id).encode()).hexdigest()[:24]
        folder.mkdir(parents=True, exist_ok=True)
        with requests.Session() as session:
            session.headers.update({'User-Agent': driver.execute_script('return navigator.userAgent'),
                                    'Referer': 'https://www.facebook.com/'})
            for cookie in driver.get_cookies():
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', '.facebook.com'))
            for url in list(dict.fromkeys(urls))[:5]:
                temporary = None
                try:
                    if stop_event.is_set():
                        break
                    if not allowed_source(url):
                        raise ValueError('Unsupported video URL')
                    started = time.monotonic()
                    with session.get(url, stream=True, timeout=(15, 30), allow_redirects=False) as response:
                        response.raise_for_status()
                        if response.is_redirect:
                            raise ValueError('Video redirect skipped')
                        content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
                        extension = {'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov'}.get(content_type)
                        if not extension:
                            raise ValueError('Response is not a supported video file')
                        destination = folder / (hashlib.sha256(url.encode()).hexdigest()[:24] + extension)
                        temporary = destination.with_suffix('.part')
                        size = 0
                        with temporary.open('wb') as stream:
                            for chunk in response.iter_content(64 * 1024):
                                if stop_event.is_set() or time.monotonic() - started > 300:
                                    raise ValueError('Video backup cancelled or timed out')
                                size += len(chunk)
                                if size > 150 * 1024 * 1024:
                                    raise ValueError('Video exceeds the 150 MB backup limit')
                                stream.write(chunk)
                        if not size:
                            raise ValueError('Empty video response')
                        temporary.replace(destination)
                        videos.append({'name': destination.name, 'path': str(destination.resolve()), 'size': size, 'mime_type': content_type})
                except Exception as exc:
                    errors.append(f'Optional video backup skipped: {exc}')
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
    except Exception as exc:
        errors.append(f'Optional video backup failed: {exc}')
    return videos, errors
