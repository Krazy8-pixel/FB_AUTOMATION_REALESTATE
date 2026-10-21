"""Listing-scoped public gallery media. Never rewrite signed CDN URLs."""
import json
import re
from urllib.parse import urlsplit

from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup


GALLERY_SCRIPT = r'''
const photos = [...document.querySelectorAll('img[alt^="Product photo of "]')];
const first = photos[0];
if (!first) return null;
// Locate this gallery, stopping before the details/recommendations area.
let root = first.parentElement;
while (root.parentElement && !root.querySelector('[aria-label^="View photo "]'))
    root = root.parentElement;
if (!root.querySelector('[aria-label^="View photo "]')) root = first.parentElement;
const buttons = [...root.querySelectorAll('[role="button"][aria-label^="View photo "]')];
const images = buttons.length ? buttons.map(b => b.querySelector('img')).filter(Boolean) : [first];
const photo = img => ({url:img.currentSrc || img.src || '',
    srcset:img.getAttribute('srcset') || '', width:img.naturalWidth, height:img.naturalHeight});
const videos = [...root.querySelectorAll('video')].map(v => ({
    urls:[v.currentSrc, v.src, ...[...v.querySelectorAll('source')].map(s=>s.src)].filter(Boolean),
    width:v.videoWidth, height:v.videoHeight}));
const videoButtons = [...root.querySelectorAll('[role="button"][aria-label]')]
    .filter(b => /^View video(?: \d+)?$/i.test(b.getAttribute('aria-label')));
return {images:images.map(photo), videos, video_buttons:videoButtons};
'''


def listing_hd_videos(html, listing_id):
    """Only use HD metadata nested in this exact listing's video collection.

    Facebook HD field names are also supported by yt-dlp's Facebook extractor.
    Never scrape every video URL on a page containing recommendations.
    """
    from automation.listing_videos import allowed_source

    def objects(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)

    urls = []
    for script in BeautifulSoup(html, 'html.parser').find_all('script', type='application/json'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in objects(payload):
            if str(node.get('id')) != str(listing_id):
                continue
            for key in ('listing_videos', 'videos'):
                for video in objects(node.get(key)):
                    url = video.get('browser_native_hd_url') or video.get('playable_url_quality_hd')
                    if url and allowed_source(url) and url not in urls:
                        urls.append(url)
    return urls


def full_photo_url(image):
    candidates = [(0, image.get('url') or '')]
    for part in (image.get('srcset') or '').split(','):
        bits = part.strip().split()
        if len(bits) == 2 and re.fullmatch(r'\d+w', bits[1]):
            candidates.append((int(bits[1][:-1]), bits[0]))
    for _, url in sorted(candidates, reverse=True):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.fbcdn.net'):
            continue
        return url
    return None


def read_public_listing_media(driver, listing_id):
    """Read only semantic product photos and optional gallery videos.

    The supplied Facebook layout exposes the complete uncropped 960px photo
    URLs even on the gallery selectors. Keep their DOM order, not the currently
    selected photo's position. Never collect arbitrary page images or videos.
    """
    data = WebDriverWait(driver, 20).until(lambda current: current.execute_script(GALLERY_SCRIPT))
    images = []
    for image in data['images']:
        url = full_photo_url(image)
        if not url:
            raise RuntimeError('Public gallery photo URL unavailable; previous photo backup kept.')
        if url not in images:
            images.append(url)
    if not images:
        raise RuntimeError('No full public listing photos found; previous photo backup kept.')
    # Activate optional video selectors so lazy players can expose their source.
    for button in data.get('video_buttons', [])[:5]:
        try:
            button.click()
            loaded = WebDriverWait(driver, 5).until(
                lambda current: (snapshot if (snapshot := current.execute_script(GALLERY_SCRIPT))
                                 and any(v.get('width') and v.get('height')
                                         for v in snapshot.get('videos', [])) else False))
            data['videos'].extend(loaded['videos'])
        except Exception:
            data.setdefault('video_issues', []).append('Optional public video did not load; skipped.')
    try:
        videos = listing_hd_videos(driver.page_source, listing_id)
    except Exception:
        videos = []
        data.setdefault('video_issues', []).append('Optional public HD video metadata could not be read; skipped.')
    metadata_hd = bool(videos)
    errors = data.get('video_issues', [])
    for video in data.get('videos', []):
        from automation.listing_videos import allowed_source
        urls = [url for url in video['urls'] if allowed_source(url)]
        if metadata_hd:
            continue  # Prefer explicitly advertised HD metadata over the player rendition.
        if urls and min(video.get('width') or 0, video.get('height') or 0) >= 720:
            videos.append(urls[0])
        else:
            errors.append('Optional public video skipped: no direct HD source available (streaming or unknown quality).')
    return {'image_urls': images, 'video_urls': list(dict.fromkeys(videos)), 'video_errors': errors}
