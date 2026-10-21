"""Local browser verification; Facebook posting and scheduling are mocked."""
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from werkzeug.serving import make_server
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_listing_routes import ListingRouteTests
from automation import listing_store


def main():
    ListingRouteTests.setUpClass()
    case = ListingRouteTests()
    case.setUp()
    Image.new('RGB', (900, 600), '#2c6da3').save(case.root / 'photo.jpg')
    output = ROOT / 'data' / 'listing_diagnostics'
    output.mkdir(parents=True, exist_ok=True)
    server = make_server('127.0.0.1', 0, case.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    drivers = list((Path.home() / '.cache/selenium/chromedriver').rglob('chromedriver.exe'))
    service = Service(str(max(drivers, key=lambda path: tuple(map(int, path.parent.name.split('.'))))))
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--window-size=1400,1000')
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    browser = webdriver.Chrome(service=service, options=options)
    wait = WebDriverWait(browser, 15, ignored_exceptions=(StaleElementReferenceException,))
    try:
        with patch.object(case.routes, 'get_all_accounts', return_value=['test-account', 'other']), \
             patch.object(case.routes.scheduler, 'add_job'), \
             patch.object(case.routes, '_runtime_log'), patch.object(case.routes, '_log_event'), \
             patch.object(case.routes, '_manual_images_directory', return_value=str(case.root / 'uploads')), \
             patch('automation.manual_backups.data_dir', return_value=case.root / 'durable'):
            browser.get(base + '/login')
            browser.add_cookie({'name': 'session', 'value': case.client.get_cookie('session').value, 'path': '/'})
            browser.get(base + '/auto_listing_manual')
            assert not browser.find_element(By.ID, 'delay_value').is_enabled()
            for field, value in {'year': '2026', 'make': 'Honda', 'model': 'City', 'mileage': '1000', 'price': '5000'}.items():
                element = browser.find_element(By.ID, field)
                element.clear()
                element.send_keys(value)
            browser.find_element(By.ID, 'image_slot_1').send_keys(str(case.root / 'photo.jpg'))
            browser.find_element(By.ID, 'manual-save-template').click()
            wait.until(lambda driver: '/backup_listings' in driver.current_url)
            browser.get(base + '/backup_listings')
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            assert not browser.find_elements(By.ID, 'facebook-panel')
            assert browser.find_element(By.CSS_SELECTOR, 'a[href="/backup_listings"]')
            browser.find_element(By.CSS_SELECTOR, '.listing-view-btn').click()
            assert browser.find_element(By.CSS_SELECTOR, '.ld-back').get_attribute('href') == base + '/backup_listings'
            browser.find_element(By.CSS_SELECTOR, '.ld-back').click()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            records = listing_store.load('test-account')['listings']
            backup = next(item for item in records if item.get('manual_template'))
            backup = dict(backup, id='identical-backup-copy')
            listing_store.add_at_top('test-account', backup)
            browser.refresh()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)

            with patch.object(case.routes, 'get_active_account', return_value='other'):
                browser.refresh()
                wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
                assert not browser.find_elements(By.ID, 'facebook-panel')
                assert browser.find_element(By.ID, 'backup-heading').text == 'Saved as backup (1)'
                assert browser.find_element(By.CSS_SELECTOR, '#backup-panel .delete-card').get_attribute('data-id') == backup['id']
            browser.get(base + '/delete_listings')
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            assert not browser.find_elements(By.ID, 'backup-panel')
            assert browser.find_element(By.ID, 'facebook-heading').text == 'Posted on Facebook (1)'
            assert not browser.find_elements(By.ID, 'listing-empty')
            assert browser.execute_script("const p=document.querySelector('#facebook-panel .listing-panel-actions'), b=[...p.querySelectorAll('button')].map(el=>el.getBoundingClientRect());return p.contains(document.getElementById('listing-import-btn')) && b.length===6 && b.every(r=>Math.abs(r.width-b[0].width)<1 && Math.abs(r.height-b[0].height)<1) && b.slice(0,3).every(r=>Math.abs(r.left-b[0].left)<1) && b.slice(3).every(r=>r.left>b[0].right) && parseFloat(getComputedStyle(document.getElementById('facebook-heading')).fontSize)>=26;")

            assert browser.execute_script("const c=document.querySelector('[data-clear-facebook]'),p=document.querySelector('#facebook-panel .listing-panel-actions'),r=c.getBoundingClientRect();return !p.contains(c) && r.top>=document.querySelector('#facebook-panel .listing-section-heading').getBoundingClientRect().bottom && Math.abs(r.right-c.parentElement.getBoundingClientRect().right)<1 && Math.abs(document.getElementById('listing-facebook-delete-btn').getBoundingClientRect().right-p.getBoundingClientRect().right)<1;")
            browser.save_screenshot(str(output / 'facebook_listings_desktop.png'))
            browser.find_element(By.CSS_SELECTOR, '[data-facebook-action="repost"]').click()
            assert browser.find_element(By.ID, 'manage-cross-dialog').is_displayed()
            assert not browser.find_elements(By.TAG_NAME, 'dialog')
            assert browser.execute_script("return document.getElementById('facebook-panel').contains(document.getElementById('manage-cross-form'))")
            assert not browser.find_element(By.ID, 'manage-cross-submit').is_enabled()
            browser.find_element(By.ID, 'listing-select-all').click()
            assert browser.find_element(By.ID, 'manage-cross-submit').is_enabled()
            assert not browser.find_element(By.ID, 'manage-target-accounts').is_displayed()
            assert browser.find_element(By.ID, 'manage-cross-delay').is_displayed()
            browser.save_screenshot(str(output / 'mass_repost_inline.png'))
            browser.find_element(By.ID, 'manage-cross-cancel').click()
            assert not browser.find_element(By.ID, 'manage-cross-dialog').is_displayed()
            browser.find_element(By.ID, 'listing-cross-post-btn').click()
            active = browser.find_element(By.CSS_SELECTOR, '[name=cross_account][value="test-account"]')
            assert not active.is_enabled() and not active.is_displayed()
            assert browser.find_element(By.CSS_SELECTOR, '[name=cross_account][value="other"]').is_displayed()
            browser.find_element(By.ID, 'manage-cross-cancel').click()
            browser.find_element(By.CSS_SELECTOR, 'a[href="/backup_listings"]').click()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            browser.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').click()
            assert browser.find_element(By.ID, 'manage-cross-dialog').is_displayed()
            assert browser.execute_script("return document.getElementById('backup-panel').contains(document.getElementById('manage-cross-form'))")
            assert browser.find_element(By.ID, 'backup-select-all').is_displayed()
            assert not browser.find_elements(By.ID, 'listing-select-all')
            assert not browser.find_elements(By.ID, 'facebook-panel')
            browser.find_element(By.ID, 'backup-select-all').click()
            assert not browser.find_elements(By.CSS_SELECTOR, '#facebook-panel .listing-check')
            active = browser.find_element(By.CSS_SELECTOR, '[name=cross_account][value="test-account"]')
            assert active.is_enabled() and active.is_displayed()
            assert not browser.find_elements(By.ID, 'backup-popup-listings')
            browser.find_element(By.ID, 'manage-account-select-all').click()
            assert all(box.is_selected() for box in browser.find_elements(By.CSS_SELECTOR, '[name=cross_account]'))
            browser.save_screenshot(str(output / 'mass_accounts_inline.png'))
            browser.find_element(By.ID, 'manage-cross-submit').click()
            wait.until(lambda driver: not driver.find_element(By.ID, 'manage-cross-dialog').is_displayed())
            assert len(case.routes.scheduled_posts) == 2
            assert all(meta['source_listing_id'] == backup['id'] for meta in case.routes.scheduled_posts.values())
            assert {meta['account'] for meta in case.routes.scheduled_posts.values()} == {'test-account', 'other'}
            for meta in case.routes.scheduled_posts.values():
                meta['status'] = 'cancelled'
            case.client.post('/auto_listing_manual', data=case.manual_form(submit_action='save_template', model='Civic'))
            browser.refresh()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 2)
            browser.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').click()
            browser.find_element(By.ID, 'backup-select-all').click()
            browser.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').click()
            browser.find_element(By.ID, 'manage-account-select-all').click()
            browser.find_element(By.ID, 'manage-cross-submit').click()
            wait.until(lambda driver: not driver.find_element(By.ID, 'manage-cross-dialog').is_displayed())
            pending = [meta for meta in case.routes.scheduled_posts.values() if meta['status'] == 'pending']
            assert len(pending) == 4
            assert len({meta['schedule_batch'] for meta in pending}) == 1
            assert {meta['schedule_position'] for meta in pending} == {0, 1}
            for account in ('test-account', 'other'):
                times = sorted(meta['run_at'] for meta in pending if meta['account'] == account)
                assert len(times) == 2 and (times[1] - times[0]).total_seconds() == 60
            for meta in pending:
                meta['status'] = 'cancelled'
            browser.refresh()
            wait.until(lambda driver: driver.find_elements(By.CSS_SELECTOR, '[data-clear-backup]')[0].is_enabled())
            browser.find_elements(By.CSS_SELECTOR, '[data-clear-backup]')[0].click()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            with patch.object(case.routes, 'get_all_accounts', return_value=[]):
                browser.refresh()
                wait.until(lambda driver: driver.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').is_enabled())
                browser.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').click()
                browser.find_element(By.ID, 'backup-select-all').click()
                browser.find_element(By.CSS_SELECTOR, '[data-backup-action="accounts"]').click()
                assert 'No saved accounts' in browser.find_element(By.ID, 'manage-target-accounts').text
                assert not browser.find_element(By.ID, 'manage-cross-submit').is_enabled()
                browser.find_element(By.ID, 'manage-cross-cancel').click()
            browser.refresh()
            wait.until(lambda driver: driver.find_element(By.CSS_SELECTOR, '[data-clear-backups]').is_enabled())
            browser.find_element(By.CSS_SELECTOR, '[data-clear-backups]').click()
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 0)
            assert len(listing_store.load('test-account')['listings']) == 1
            browser.get(base + '/delete_listings')
            wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 1)
            browser.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            assert browser.execute_script('return document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            browser.save_screenshot(str(output / 'manual_templates_mobile.png'))
            browser.find_element(By.ID, 'listing-cross-post-btn').click()
            assert browser.find_element(By.ID, 'manage-cross-dialog').is_displayed()
            assert browser.execute_script('return document.documentElement.scrollWidth <= innerWidth'), 'Inline form mobile overflow'
            browser.save_screenshot(str(output / 'mass_post_inline_mobile.png'))
            browser.find_element(By.ID, 'manage-cross-cancel').click()
            with patch.object(case.routes, 'delete_selected_listings_from_facebook') as facebook_delete:
                clear_local = browser.find_element(By.CSS_SELECTOR, '[data-clear-facebook]')
                browser.execute_script("arguments[0].scrollIntoView({block:'center'});", clear_local)
                clear_local.click()
                wait.until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, '.delete-card')) == 0)
                facebook_delete.assert_not_called()

            errors = [entry['message'] for entry in browser.get_log('browser')
                      if entry['level'] == 'SEVERE' and 'favicon.ico' not in entry['message']]
            assert not errors, errors
            print('PASS: manual template upload/save, separate Facebook and Saved Backups pages, sidebar navigation, shared delayed backup posting, backup-only clearing, and mobile layout. No Facebook actions performed.')
    finally:
        browser.quit()
        server.shutdown()
        case.tearDown()


if __name__ == '__main__':
    main()
