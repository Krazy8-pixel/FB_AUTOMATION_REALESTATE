"""Exercise cross posting in a local browser with mocked Facebook posting."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from automation.bbc import chrome_executable


def main():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('listing_route_tests', root / 'tests/test_listing_routes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = module.ListingRouteTests()
    case.setUpClass()
    case.setUp()
    from PIL import Image
    Image.new('RGB', (1000, 800), '#dbeafe').save(case.root / 'photo.jpg')
    errors = []
    try:
        with patch.object(case.routes, 'get_all_accounts', return_value=['test-account', 'other', 'third']), \
             patch.object(case.routes.scheduler, 'add_job'), \
             patch.object(case.routes, 'post_to_fb_marketplace', return_value=True) as poster, \
             patch.object(case.routes, '_runtime_log'), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome_executable(), headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname != '127.0.0.1':
                    route.abort()
                    return
                response = case.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                                            method=request.method, data=request.post_data,
                                            content_type=request.headers.get('content-type'))
                route.fulfill(status=response.status_code, body=response.data,
                              content_type=response.content_type)

            page.route('**/*', serve)
            page.goto('http://127.0.0.1:5001/delete_listings/detail/one')
            page.locator('#start-cross-post').click()
            assert page.locator('#cross-post-accounts').is_visible()
            assert page.locator('[data-cross-account]').count() == 2
            assert page.locator('[data-cross-account][value="test-account"]').count() == 0
            page.locator('#cross-select-all').click()
            assert page.locator('[data-cross-account]:checked').count() == 2
            page.locator('input[name="title"]').fill('Cross-posted vehicle')
            page.locator('input[name="price"]').fill('500')
            page.locator('#confirm-repost').click()
            try:
                page.wait_for_function("document.getElementById('workflow-status').textContent.includes('Cross posting queued')", timeout=5000)
            except Exception:
                print('Submission feedback:', page.locator('#workflow-status').inner_text())
                print('Browser errors:', errors)
                print('Invalid fields:', page.locator('#repost-form').evaluate("form => [...form.querySelectorAll(':invalid')].map(el => [el.name, el.validationMessage])"))
                print('Queued jobs:', list(case.routes.scheduled_posts))
                raise
            ids = list(case.routes.scheduled_posts)
            case.routes._run_cross_post_batch(ids)
            assert poster.call_count == 2
            assert poster.call_args_list[0].args[0]['title'] == 'Cross-posted vehicle'
            assert poster.call_args_list[0].args[0]['price'] == '500'
            page.goto('http://127.0.0.1:5001/post_schedular')
            page.wait_for_function("document.getElementById('schedular-rows').textContent.includes('other')")
            assert page.locator('#schedular-rows').inner_text().count('posted') >= 2
            assert not errors, errors
            output = root / 'outputs/cross_post_ui'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'scheduled_posts.png'), full_page=True)
            page.goto('http://127.0.0.1:5001/delete_listings/detail/one')
            page.locator('#start-cross-post').click()
            page.screenshot(path=str(output / 'listing_details.png'), full_page=True)
            case.routes.update_listing_fields('test-account', 'one', {
                'facebook_present': False, 'source_deleted': True, 'facebook_reposted': False})
            page.goto('http://127.0.0.1:5001/delete_listings/detail/one')
            assert not page.locator('#listing-facebook-status').is_visible()
            assert page.locator('.listing-backup-badge').is_visible()
            page.goto('http://127.0.0.1:5001/delete_listings')
            assert page.locator('.listing-source-badge:visible').count() == 0
            assert page.locator('.listing-backup-badge').is_visible()
            browser.close()
            print('Cross-post browser workflow passed; no Facebook posts were sent.')
    finally:
        case.tearDown()


if __name__ == '__main__':
    main()
