"""Offline browser checks against Flask test data; all posting is mocked."""
from pathlib import Path
import json
import sqlite3
import sys
from urllib.parse import urlsplit
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_listing_routes import ListingRouteTests
from automation import listing_store
from automation.bbc import chrome_executable
from playwright.sync_api import sync_playwright


def main():
    # Use a copy of repaired source data for an accurate preview, in a temporary DB.
    with sqlite3.connect(f'file:{(ROOT / "data/listings.db").as_posix()}?mode=ro', uri=True) as db:
        sources = [json.loads(row[0]) for row in db.execute('SELECT data FROM listings ORDER BY position')]
        source = next(item for item in sources if item['id'] == 'b850e2d350c5113b7932')
    ListingRouteTests.setUpClass()
    fixture = ListingRouteTests()
    fixture.setUp()
    source['id'] = 'one'
    source['repost_preferences'] = {}
    source['source_deleted'] = False
    source['repost_status'] = ''
    output = ROOT / 'data/listing_diagnostics'
    output.mkdir(exist_ok=True)
    try:
        listing_store.save('test-account', [source])
        with patch.object(fixture.routes.scheduler, 'add_job') as scheduler, patch.object(fixture.routes, '_runtime_log'), sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=chrome_executable(), headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            delete_runners = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route_request(route):
                request = route.request
                url = urlsplit(request.url)
                def open_request():
                    return fixture.client.open(url.path + ('?' + url.query if url.query else ''),
                        method=request.method, data=request.post_data,
                        content_type=request.headers.get('content-type'))
                if url.path == '/delete_listings/delete' and request.method == 'POST':
                    def hold_worker(*args, **kwargs):
                        delete_runners.append(kwargs['target'])
                        return MagicMock()
                    with patch.object(fixture.routes.threading, 'Thread', side_effect=hold_worker):
                        response = open_request()
                else:
                    response = open_request()
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)
                response.close()
            page.route('**/*', route_request)
            page.goto('http://listing-test.local/')
            page.wait_for_load_state('networkidle')
            assert page.locator('.simple-dashboard').is_visible()
            assert page.locator('a[href*="auto_reply"], a[href*="marketplace_vehicle_messenger"], a[href*="email_setter"]').count() == 0
            page.screenshot(path=str(output / 'dashboard_without_messaging.png'), full_page=True)
            for path in ('/auto_reply', '/auto_reply_status', '/auto_reply_status_data', '/marketplace_vehicle_messenger', '/marketplace_vehicle_messenger/status', '/email_setter'):
                assert fixture.client.get(path).status_code == 404, path
            for path in ('/auto_reply', '/auto_reply_stop', '/marketplace_vehicle_messenger/start', '/marketplace_vehicle_messenger/stop'):
                assert fixture.client.post(path).status_code == 404, path
            page.goto('http://listing-test.local/delete_listings/detail/one')
            page.wait_for_load_state('networkidle')
            assert page.locator('#repost-form').is_hidden()
            assert page.locator('#listing-read-view').is_visible()
            assert page.locator('#listing-hero').evaluate('(img) => img.complete && img.naturalWidth > 0')
            assert page.locator('.ld-header h1').inner_text() == '2021 Honda civic'
            assert page.locator('.ld-info-grid div').filter(has=page.locator('dt', has_text='Model')).locator('dd').inner_text() == 'civic'
            assert page.locator('.ld-back').evaluate('(el) => getComputedStyle(el).borderTopStyle') == 'solid'
            assert page.locator('#listing-facebook-status').inner_text() == 'Posted on Facebook'
            assert page.locator('.listing-backup-badge').inner_text() == 'Saved as backup'
            for width in (1440, 1200, 1024, 900, 768, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}'
                for selector in ('.ld-header', '.ld-gallery', '.ld-information', '.ld-back'):
                    box = page.locator(selector).bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (width, selector, box)
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(output / 'details_desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            page.screenshot(path=str(output / 'details_mobile.png'), full_page=True)
            # The Civic has one photo. Verify reordering using an unchanged
            # Transit listing with multiple photos, still in the temporary DB.
            source = next(item for item in sources if item['id'] == '12845c2b42cdebf1a4c1')
            source['id'] = 'one'
            source['repost_preferences'] = {}
            source['source_deleted'] = False
            source['repost_status'] = ''
            listing_store.save('test-account', [source])
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.goto('http://listing-test.local/delete_listings/detail/one')
            page.wait_for_load_state('networkidle')
            page.screenshot(path=str(output / 'details_transit_desktop.png'), full_page=True)
            page.locator('#start-repost').click()
            assert page.locator('#repost-form').is_visible()
            assert page.locator('#listing-read-view').is_hidden()
            initial_photos = page.locator('[data-photo]').evaluate_all('(rows) => rows.map(row => row.dataset.photo)')
            third = initial_photos[2]
            page.locator('[data-photo]').nth(2).locator('.ld-order-image').drag_to(
                page.locator('[data-photo]').first.locator('.ld-order-image'), target_position={'x': 8, 'y': 8})
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == third
            assert page.locator('[data-photo]').first.locator('.ld-order-label').inner_text() == 'Cover photo'
            last_image = page.locator('[data-photo]').last.locator('.ld-order-image')
            last_box = last_image.bounding_box()
            page.locator('[data-photo]').first.locator('.ld-order-image').drag_to(
                last_image, target_position={'x': last_box['width']-8, 'y': last_box['height']-8})
            assert page.locator('[data-photo]').last.get_attribute('data-photo') == third
            page.locator('[data-photo]').last.locator('.ld-order-image').drag_to(
                page.locator('[data-photo]').first.locator('.ld-order-image'), target_position={'x': 8, 'y': 8})
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == third
            # Exercise a real touch gesture through Chrome's input pipeline.
            page.set_viewport_size({'width': 390, 'height': 844})
            if not page.locator('body').evaluate("el => el.classList.contains('mobile-header-collapsed')"):
                page.locator('#mobile-header-toggle').click()
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            page.locator('[data-photo]').first.evaluate('''row => {
                const offset = document.querySelector('.topbar').getBoundingClientRect().height
                    + document.querySelector('.sidebar').getBoundingClientRect().height + 20;
                window.scrollBy(0, row.getBoundingClientRect().top - offset);
            }''')
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            touch_second = page.locator('[data-photo]').nth(1).get_attribute('data-photo')
            grip_box = page.locator('[data-photo]').nth(1).locator('.ld-drag-handle').bounding_box()
            target_box = page.locator('[data-photo]').first.locator('.ld-order-image').bounding_box()
            sx, sy = grip_box['x'] + grip_box['width']/2, grip_box['y'] + grip_box['height']/2
            tx, ty = target_box['x'] + 8, target_box['y'] + 8
            assert page.evaluate('''([x,y]) => !!document.elementFromPoint(x,y)?.closest('.ld-drag-handle')''', [sx, sy]), (sx, sy, grip_box)
            touch = page.context.new_cdp_session(page)
            touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': sx, 'y': sy}]})
            for step in range(1, 6):
                touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': sx+(tx-sx)*step/5, 'y': sy+(ty-sy)*step/5}]})
            touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
            touch.detach()
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == touch_second, (sx, sy, tx, ty)
            assert page.locator('.ld-drag-preview').count() == 0
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.locator('#cancel-repost').click()
            page.locator('#start-repost').click()
            assert page.locator('[data-photo]').evaluate_all('(rows) => rows.map(row => row.dataset.photo)') == initial_photos
            page.locator('input[name="price"]').fill('30000')
            second = page.locator('[data-photo]').nth(1).get_attribute('data-photo')
            page.locator('[data-photo]').nth(1).locator('[data-move="first"]').click()
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == second
            page.locator('[data-photo]').first.locator('[data-move="down"]').click()
            assert page.locator('[data-photo]').nth(1).get_attribute('data-photo') == second
            page.locator('[data-photo]').nth(1).locator('[data-move="up"]').click()
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == second
            page.locator('#cancel-repost').click()
            assert page.locator('#repost-form').is_hidden()
            page.locator('#start-repost').click()
            assert page.locator('input[name="price"]').input_value() == source['details']['price']
            page.locator('[data-photo]').nth(1).locator('.ld-order-image').drag_to(
                page.locator('[data-photo]').first.locator('.ld-order-image'), target_position={'x': 8, 'y': 8})
            assert page.locator('[data-photo]').first.get_attribute('data-photo') == second
            page.locator('input[name="price"]').fill('30000')
            page.locator('input[name="post_mode"][value="schedule"]').check()
            page.locator('#repost-delay-value').fill('2')
            page.locator('#repost-delay-unit').select_option('hours')
            assert page.locator('#schedule-fields').is_visible()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile editor overflow'
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(output / 'repost_desktop.png'), full_page=True)
            page.locator('#confirm-repost').click()
            page.wait_for_function("document.getElementById('repost-form').hidden")
            assert scheduler.called
            frozen = scheduler.call_args.kwargs['args'][0]
            assert frozen['repost_preferences']['fields']['price'] == '30000'
            assert frozen['repost_preferences']['image_order'][0] == second
            page.goto('http://listing-test.local/delete_listings')
            page.wait_for_load_state('networkidle')
            assert page.locator('.listing-view-btn').count() == 1
            assert page.locator('.listing-state').inner_text() == 'Scheduled'
            assert page.locator('.listing-repost-btn').count() == 0
            assert page.locator('.listing-source-badge').inner_text() == 'Posted on Facebook'
            assert page.locator('.listing-backup-badge').inner_text() == 'Saved as backup'
            assert page.locator('.listing-title').evaluate('(el) => getComputedStyle(el).textDecorationLine') == 'none'
            card = page.locator('.delete-card')
            normal_border = card.evaluate('(el) => getComputedStyle(el).borderTopColor')
            card.hover()
            page.wait_for_function("getComputedStyle(document.querySelector('.delete-card')).borderTopColor === 'rgb(11, 61, 145)'")
            assert card.evaluate('(el) => getComputedStyle(el).borderTopColor') != normal_border
            page.screenshot(path=str(output / 'all_listings_status_hover.png'), full_page=True)
            fixture.routes.repost_jobs.clear()
            for target in ('facebook', 'database', 'both'):
                listing_store.save('test-account', [source])
                page.goto('http://listing-test.local/delete_listings/detail/one')
                page.wait_for_load_state('networkidle')
                page.locator('#start-delete').click()
                assert page.locator('#delete-options').is_visible()
                assert page.locator('[data-delete-target]').count() == 3
                if target == 'facebook':
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path=str(output / 'delete_choices_desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / 'delete_choices_mobile.png'), full_page=True)
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                page.locator(f'[data-delete-target="{target}"]').click()
                if target != 'database':
                    page.wait_for_function("document.getElementById('start-delete').textContent === 'Deleting…'")
                    assert page.locator('#delete-options').is_hidden()
                    assert page.locator('#start-repost').is_disabled()
                    assert page.locator('.delete-operation-overlay').count() == 0
                    with patch.object(fixture.routes, 'delete_selected_listings_from_facebook',
                                      return_value={'deleted': 1, 'deleted_ids': ['one'], 'clicked': 1}):
                        delete_runners.pop(0)()
                if target == 'facebook':
                    page.wait_for_function("document.getElementById('start-delete').textContent === 'Delete listing' && !document.getElementById('start-repost').disabled")
                    backup = listing_store.load('test-account')['listings'][0]
                    assert backup['source_deleted']
                    assert backup['details'] == source['details']
                    assert len(backup['downloaded_images']) == len(source['downloaded_images'])
                    assert page.locator('#listing-facebook-status').inner_text() == 'Removed from Facebook'
                    assert page.locator('.listing-backup-badge').inner_text() == 'Saved as backup'
                else:
                    page.wait_for_url('**/delete_listings')
                    assert listing_store.load('test-account')['listings'] == []
            fixture.routes.repost_jobs.clear()
            for job_id, state in [('future', 'pending'), ('running', 'posting'), ('removing', 'deleting')]:
                fixture.routes.repost_jobs[job_id] = {'job_id':job_id, 'listing_id':'one', 'account':'test-account',
                    'title':job_id+' listing', 'status':state, 'run_at':datetime.now()+timedelta(hours=2),
                    'delay_value':2, 'delay_unit':'hours'}
            fixture.routes._save_repost_history([
                {'account':'test-account','listing_id':'one','title':'Failed vehicle','status':'failed',
                 'timestamp':'2026-10-11 19:00:00','error':'Body style: unavailable option <original>'},
                {'account':'test-account','listing_id':'one','title':'Completed vehicle','status':'reposted',
                 'timestamp':'2026-10-11 18:00:00','error':''}])
            page.set_viewport_size({'width':1440,'height':1000})
            page.locator('.sidebar a', has_text='Repost Status').click()
            page.wait_for_url('**/reposting')
            page.wait_for_load_state('networkidle')
            assert page.locator('#repost-count').inner_text() == '3'
            assert page.locator('#repost-pending-count').inner_text() == '1'
            assert page.locator('#repost-running-count').inner_text() == '2'
            assert page.locator('#repost-status-rows tr').count() == 3
            assert 'Scheduled' in page.locator('#repost-status-rows').inner_text()
            assert 'Deleting original listing' in page.locator('#repost-status-rows').inner_text()
            assert 'Body style: unavailable option <original>' in page.locator('#repost-history-rows').inner_text()
            assert page.locator('.repost-state.reposted').count() == 1
            assert page.locator('.repost-state.failed').count() == 1
            page.screenshot(path=str(output/'repost_status_desktop.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'repost_status_mobile.png'), full_page=True)
            assert not errors, errors
            print('Browser checks passed: restored repost status, schedules and results, mouse/touch photo dragging, responsive layouts, delete choices, retained backups, zero JS errors.')
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
