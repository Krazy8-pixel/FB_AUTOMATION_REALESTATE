"""Render the dashboard against isolated test data without Facebook access."""
import sys
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_listing_routes import ListingRouteTests
from playwright.sync_api import sync_playwright
from automation.bbc import chrome_executable


def main():
    ListingRouteTests.setUpClass()
    fixture = ListingRouteTests()
    fixture.setUp()
    output = ROOT / 'outputs/dashboard-review'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome_executable(), headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                url = urlsplit(route.request.url)
                if url.hostname != '127.0.0.1':
                    route.abort()
                    return
                response = fixture.client.get(url.path)
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

            page.route('**/*', serve)
            page.goto('http://127.0.0.1/')
            page.screenshot(path=str(output / 'dashboard-desktop.png'), full_page=True)
            search = page.locator('#dashboard-tool-search')
            search.fill('groups')
            assert page.locator('.simple-feature-list a:visible').count() == 2
            search.fill('no matching tool')
            assert page.locator('#dashboard-no-results').is_visible()
            search.fill('')
            assert page.locator('.simple-feature-list a:visible').count() == 8
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(output / 'dashboard-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            with patch.object(fixture.routes, 'get_active_account', return_value=None):
                page.goto('http://127.0.0.1/')
                assert page.locator('.dashboard-primary').inner_text().endswith('Add an account')
            page.set_viewport_size({'width': 1440, 'height': 1100})
            page.goto('http://127.0.0.1/delete_listings')
            assert page.locator('.listing-price').first.inner_text() == '100'
            page.screenshot(path=str(output / 'listings-desktop.png'), full_page=True)
            assert page.locator('.delete-card').first.evaluate('(el) => getComputedStyle(el).borderRadius') == '0px'
            assert not errors, errors
            browser.close()
        print('Dashboard desktop/mobile, search, empty account, and listing price checks passed.')
        print(output)
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
