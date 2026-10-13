"""Read-only live verification of stored edit pages; never submits Facebook forms."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation.runtime_paths import data_dir
from automation.reposting import _open_prelogged_facebook
from automation.reposting import read_edit_form_snapshot, _safe_image_urls, highest_resolution_listing_images
from automation.delete_listing_import import download_listing_images
from automation.listing_store import update_fields
import threading
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--listing-id', default='')
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--inspect-public', action='store_true')
    args = parser.parse_args()
    db = sqlite3.connect(str(data_dir() / 'listings.db'))
    rows = db.execute('SELECT account,id,data FROM listings').fetchall()
    db.close()
    folder = data_dir('listing_diagnostics')
    for account, listing_id, data in rows:
        if args.listing_id and listing_id != args.listing_id:
            continue
        listing = json.loads(data)
        if not listing.get('edit_url'):
            continue
        driver = None
        try:
            with (folder / 'verification.log').open('a', encoding='utf-8') as log, redirect_stdout(log):
                driver = _open_prelogged_facebook(account, force_headless=True)
                driver.get(listing['edit_url'])
                from selenium.webdriver.support.ui import WebDriverWait
                WebDriverWait(driver, 30).until(lambda d: len(d.find_elements('css selector', 'input,textarea,[contenteditable="true"]')) > 3)
                time.sleep(4)
                controls = driver.execute_script('''
                    return [...document.querySelectorAll('label,input,textarea,[contenteditable="true"],[role="combobox"]')]
                      .map(el => ({tag:el.tagName, role:el.getAttribute('role'), aria:el.getAttribute('aria-label'),
                        text:el.innerText || '', value:el.value || '', disabled:el.disabled || el.getAttribute('aria-disabled'),
                        html:el.outerHTML.slice(0,6000)}));
                ''')
                verified_edit_page = '/marketplace/edit/' in driver.current_url
                (folder / (listing_id + '_controls.json')).write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding='utf-8')
                images = driver.execute_script('''return [...document.querySelectorAll('[role="main"] img')].map(img => ({src:img.currentSrc || img.src, srcset:img.srcset, width:img.naturalWidth, height:img.naturalHeight, preview:!!img.closest('[aria-label="Preview"]'), html:img.outerHTML}));''')
                (folder / (listing_id + '_images.json')).write_text(json.dumps(images, ensure_ascii=False, indent=2), encoding='utf-8')
                if args.inspect_public:
                    from urllib.parse import urlsplit, parse_qs
                    facebook_id = parse_qs(urlsplit(listing['edit_url']).query)['listing_id'][0]
                    driver.get('https://www.facebook.com/marketplace/item/' + facebook_id + '/')
                    time.sleep(5)
                    public = driver.execute_script('''return {
                        images:[...document.querySelectorAll('[role="main"] img')].map(img => ({src:img.currentSrc || img.src, width:img.naturalWidth, height:img.naturalHeight, html:img.parentElement.outerHTML.slice(0,6000)})),
                        buttons:[...document.querySelectorAll('[role="main"] [role="button"]')].map(el => ({aria:el.getAttribute('aria-label'),text:el.innerText,html:el.outerHTML.slice(0,2500)}))};''')
                    (folder / (listing_id + '_public.json')).write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
                if args.repair:
                    snapshot = read_edit_form_snapshot(driver)
                    details = dict(listing.get('details') or {})
                    details.update({field: state['value'] for field, state in snapshot.items() if field != 'model'})
                    details['field_states'] = {field: state['state'] for field, state in snapshot.items()}
                    details['image_urls'] = _safe_image_urls(driver)
                    details['image_urls'] = highest_resolution_listing_images(driver, listing['edit_url'], details['image_urls'])
                    photos, errors = download_listing_images(driver, account, listing_id, details['image_urls'], threading.Event())
                    update_fields(account, listing_id, {'details': details, 'downloaded_images': photos, 'image_errors': errors,
                        'detail_error': '' if photos and not errors else 'Photo import incomplete. Import this listing again.',
                        'details_imported_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
            print(json.dumps({'listing_id': listing_id, 'controls': len(controls), 'verified_edit_page': verified_edit_page}), flush=True)
            if args.repair:
                print(json.dumps({'listing_id': listing_id, 'field_states': details['field_states'], 'photos_saved': len(photos), 'photo_errors': len(errors)}), flush=True)
        finally:
            if driver:
                driver.quit()


if __name__ == '__main__':
    main()
