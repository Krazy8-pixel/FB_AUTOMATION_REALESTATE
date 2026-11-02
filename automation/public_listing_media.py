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
// The main image sits inside its own View photo button. Stop there and only
// one photo is found. Climb to the smallest ancestor holding multiple photo
// controls; single-photo listings retain the nearest one-button gallery.
let root = first.parentElement;
let bestRoot = null;
let bestPhotoButtonCount = 0;
while (root && root !== document.body) {
    const count = root.querySelectorAll('[role="button"][aria-label^="View photo "]').length;
    if (count > bestPhotoButtonCount) {
        bestRoot = root;
        bestPhotoButtonCount = count;
    }
    if (count > 1) {
        bestRoot = root;
        break;
    }
    root = root.parentElement;
}
root = bestRoot || first.parentElement;
const buttons = [...root.querySelectorAll('[role="button"][aria-label^="View photo "]')];
const images = buttons.length ? buttons.map(b => b.querySelector('img')).filter(Boolean) : [first];
const photo = img => ({url:img.currentSrc || img.src || '',
    srcset:img.getAttribute('srcset') || '', width:img.naturalWidth, height:img.naturalHeight});
const videos = [...root.querySelectorAll('video')].map(v => ({
    urls:[v.currentSrc, v.src, ...[...v.querySelectorAll('source')].map(s=>s.src)].filter(Boolean),
    width:v.videoWidth, height:v.videoHeight}));
const videoButtons = [...root.querySelectorAll('[role="button"][aria-label]')]
    .filter(b => /^View video(?: \d+)?$/i.test(b.getAttribute('aria-label')))
    .map(b => b.getAttribute('aria-label'));
return {images:images.map(photo), videos, video_buttons:videoButtons};
'''

# Facebook may render the player in a modal elsewhere in the document after
# the listing's View video control is clicked, outside the photo gallery root.
VISIBLE_VIDEO_SCRIPT = r'''
return [...document.querySelectorAll('video')].filter(v => {
    const r = v.getBoundingClientRect(), s = getComputedStyle(v);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
}).map(v => ({
    urls:[v.currentSrc, v.getAttribute('src'),
          ...[...v.querySelectorAll('source')].map(s => s.src || s.getAttribute('src'))].filter(Boolean),
    width:v.videoWidth, height:v.videoHeight, readyState:v.readyState
}));
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
    """Read only semantic product photos and any video in this listing's gallery.

    The supplied Facebook layout exposes the complete uncropped 960px photo
    URLs even on the gallery selectors. Keep their DOM order, not the currently
    selected photo's position. Never collect arbitrary page images or videos.
    """
    data = WebDriverWait(driver, 30).until(lambda current: current.execute_script(GALLERY_SCRIPT))
    images = []
    for image in data['images']:
        url = full_photo_url(image)
        if not url:
            raise RuntimeError('Public gallery photo URL unavailable; previous photo backup kept.')
        if url not in images:
            images.append(url)
    if not images:
        raise RuntimeError('No full public listing photos found; previous photo backup kept.')
    # Activate video controls so Facebook has a chance to mount its player.
    for label in data.get('video_buttons', [])[:5]:
        try:
            # Keep labels, not Selenium element references: Facebook may
            # replace the gallery DOM between reading it and clicking a video.
            button = WebDriverWait(driver, 15).until(lambda current: next((
                element for element in current.find_elements(
                    'css selector', '[role="button"][aria-label]')
                if element.get_attribute('aria-label') == label
            ), False))
            button.click()
            # Player is often mounted in a modal outside the photo gallery.
            # Wait for a visible player and source, without requiring dimensions:
            # some Facebook players report videoWidth/videoHeight late or as 0.
            loaded_videos = WebDriverWait(driver, 30).until(lambda current: (
                found if (found := current.execute_script(VISIBLE_VIDEO_SCRIPT))
                and any(video.get('urls') for video in found) else False))
            data['videos'].extend(loaded_videos)
        except Exception:
            data.setdefault('video_issues', []).append(
                f'Listing has a {label} control, but its video player/source was not available after waiting.')
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
        # A valid direct CDN source is enough to capture the video. Facebook
        # may leave dimensions at zero even after the source has loaded.
        if urls:
            videos.append(urls[0])
        else:
            errors.append('Listing video was detected, but Facebook exposed no direct downloadable source.')
    return {'image_urls': images, 'video_urls': list(dict.fromkeys(videos)), 'video_errors': errors}
