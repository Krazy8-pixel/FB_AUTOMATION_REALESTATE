"""Opt-in live OCR test. Does not save listing records or submit Facebook forms."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, help='An existing saved account title')
    parser.add_argument('--edit-url', required=True, help='Exact Facebook Marketplace edit URL')
    parser.add_argument('--output', default='outputs/visual-listing-reader')
    parser.add_argument('--download-images', action='store_true')
    args = parser.parse_args()
    from urllib.parse import urlsplit
    url = urlsplit(args.edit_url)
    if url.scheme != 'https' or url.hostname not in ('facebook.com', 'www.facebook.com') or not url.path.startswith('/marketplace/edit/'):
        parser.error('Use an HTTPS Facebook Marketplace edit URL')
    from automation.visual_listing_reader import read_listing_edit_visual
    from automation.reposting import _open_prelogged_facebook
    driver = _open_prelogged_facebook(args.account)
    try:
        driver.get(args.edit_url)
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState==='complete' && !!document.querySelector('input,textarea');"))
        result = read_listing_edit_visual(driver, args.output)
        if args.download_images:
            from automation import delete_listing_import as media
            # Reuse original-byte downloads, but isolate files from real backups.
            media.IMPORT_DIR = Path(args.output).resolve() / 'media'
            result['downloaded_images'], result['image_errors'] = media.download_listing_images(
                driver, args.account, 'ocr-test', result['posts'][0]['image_urls'], threading.Event())
        report = Path(args.output) / 'report.json'
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print('Review screenshots and extracted values:', report.resolve())
        print('No production records changed. Video URLs are extracted, not downloaded.')
    finally:
        driver.quit()


if __name__ == '__main__':
    main()
