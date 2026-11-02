import threading
import unittest
from urllib.parse import quote

from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver
from automation import delete_listing_import as importer


class DeleteSearchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        cls.driver = build_chrome_driver(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def test_searches_supplied_input_and_finds_matching_vehicle_card(self):
        html = '''<div role="main"><h1>Your listings</h1>
        <label><span><svg></svg></span><input placeholder="Search your listings"
          aria-label="Search your listings" type="text" value=""></label>
        <div id="result" style="display:none">
          <input aria-label="Select Listing" type="checkbox">
          <div role="button" aria-label="2018 Dodge Challenger">2018 Dodge Challenger</div>
          <div>MAD 21,000</div><div role="button" aria-label="More options for 2018 Dodge Challenger"></div>
        </div></div>
        <script>
          document.querySelector('input[placeholder]').addEventListener('input', event => {
            document.getElementById('result').style.display =
              event.target.value === '2018 Dodge Challenger' ? 'block' : 'none';
          });
        </script>'''
        self.driver.get('data:text/html,' + quote(html))
        self.assertTrue(self.driver.find_elements('css selector', 'input[placeholder]'),
                        self.driver.page_source[:500])
        listing = {'id': 'one', 'year': '2018', 'make': 'Dodge', 'model': 'Challenger',
                   'price': '21000', '_match_by_vehicle_details_only': True}
        candidate = importer._search_delete_candidate(self.driver, listing, threading.Event(), timeout=5)
        self.assertEqual(candidate['title'], '2018 Dodge Challenger')
        self.assertTrue(candidate['element'].is_displayed())


if __name__ == '__main__':
    unittest.main()
