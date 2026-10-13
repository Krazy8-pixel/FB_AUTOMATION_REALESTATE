"""Check desktop Settings and help interactions without contacting Facebook."""
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
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
    errors = []
    output = root / 'outputs/desktop-ui'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with patch('automation.desktop_preferences.data_dir', return_value=case.root), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome_executable(), headless=True)
            page = browser.new_page(viewport={'width': 1380, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname != '127.0.0.1':
                    route.abort()
                    return
                response = case.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''), method=request.method, data=request.post_data, content_type=request.headers.get('content-type'))
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

            page.route('**/*', serve)
            page.goto('http://127.0.0.1:5001/settings')
            assert page.locator('[name=background_sync]').is_checked()
            assert page.locator('[name=start_with_windows]').is_checked()
            brand = page.locator('.topbar .tayyara-wordmark').bounding_box()
            dropdown = page.locator('.header-account-select').bounding_box()
            assert brand['x'] + brand['width'] <= dropdown['x']
            page.screenshot(path=str(output / 'settings-desktop.png'), full_page=True)
            page.locator('[name=background_sync]').uncheck()
            page.get_by_role('button', name='Save preferences').click()
            assert not page.locator('[name=background_sync]').is_checked()
            page.set_viewport_size({'width': 390, 'height': 600})
            page.reload()
            page.screenshot(path=str(output / 'settings-mobile.png'), full_page=False, animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto('http://127.0.0.1:5001/info')
            page.locator('#guide-search').fill('updates')
            assert page.locator('#desktop').is_visible()
            page.get_by_role('button', name='EXPAND ALL').click()
            assert page.locator('#desktop').get_attribute('open') is not None
            assert 'domain checker' not in page.locator('main').inner_text().lower()
            page.screenshot(path=str(output / 'help-mobile.png'), full_page=False, animations='disabled')
            assert not errors, errors
            browser.close()
            print('Settings preferences, header spacing, mobile layout, and interactive guide passed.')
    finally:
        case.tearDown()


if __name__ == '__main__':
    main()
