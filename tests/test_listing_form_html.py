import unittest
from unittest.mock import MagicMock
from urllib.parse import quote
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver
from automation.reposting import read_edit_form_snapshot, wait_for_edit_form_ready, highest_resolution_listing_images, _find_edit_listing_link, _model_from_visible_listing_heading, _valid_model_text


class ListingFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        cls.driver = build_chrome_driver(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def test_reads_values_and_distinguishes_blank_absent_and_disabled(self):
        html = '''<main role="main">
          <label role="combobox"><span>Vehicle type</span><span>Car/van</span></label>
          <label><span>Make</span><input value="Ford"></label>
          <input aria-label="Location" value="Islamabad">
          <label role="combobox" aria-disabled="true"><span>Exterior colour</span><span>Gray</span></label>
          <label><span>Description</span><textarea></textarea></label>
          <input type="checkbox" aria-label="This vehicle has a clean title." checked>
        </main>'''
        self.driver.get('data:text/html,' + quote(html))
        values = read_edit_form_snapshot(self.driver)
        self.assertEqual(values['make'], {'value': 'Ford', 'state': 'read'})
        self.assertEqual(values['location']['value'], 'Islamabad')
        self.assertEqual(values['description']['state'], 'empty')
        self.assertEqual(values['interior_color']['state'], 'unavailable')
        self.assertEqual(values['exterior_color'], {'value': 'Gray', 'state': 'disabled'})
        self.assertEqual(values['clean_title']['value'], 'Yes')

    def test_contenteditable_description_and_sibling_span_edit_fallback(self):
        html = '''<main role="main"><div><a href="https://www.facebook.com/marketplace/edit/?listing_id=123">Icon</a><div><span>Edit listing</span></div></div>
          <label><span>Description</span><div contenteditable="true">Full vehicle description</div></label></main>'''
        self.driver.get('data:text/html,' + quote(html))
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Full vehicle description')
        link = _find_edit_listing_link(self.driver, timeout=1)
        self.assertIsNotNone(link)
        self.assertIn('listing_id=123', link.get_attribute('href'))

    def test_waits_for_delayed_populated_form_instead_of_empty_main(self):
        html = '''<main role="main"></main><script>
          setTimeout(() => document.querySelector('main').innerHTML =
            '<label for="price">Price</label><input id="price" value="2500">' +
            '<span id="make-label">Make</span><input aria-labelledby="make-label" value="Ford">' +
            '<textarea aria-label="Description">Full description</textarea>', 700);
        </script>'''
        self.driver.get('data:text/html,' + quote(html))
        snapshot = wait_for_edit_form_ready(self.driver, timeout=6)
        self.assertEqual(snapshot['price']['value'], '2500')
        self.assertEqual(snapshot['make']['value'], 'Ford')

    def test_unrendered_form_fails_clearly(self):
        self.driver.get('data:text/html,' + quote('<main role="main">Loading...</main>'))
        with self.assertRaisesRegex(RuntimeError, 'did not finish loading'):
            wait_for_edit_form_ready(self.driver, timeout=1)

    def test_full_size_gallery_matches_only_source_photos_and_keeps_order(self):
        driver = MagicMock()
        driver.current_window_handle = 'edit-tab'
        driver.execute_script.return_value = [
            {'url': 'https://scontent.test.fbcdn.net/one.jpg?full=yes', 'width': 1600, 'height': 1200},
            {'url': 'https://scontent.test.fbcdn.net/one.jpg?thumb=yes', 'width': 260, 'height': 260},
            {'url': 'https://scontent.test.fbcdn.net/two.jpg?full=yes', 'width': 1200, 'height': 1600},
            {'url': 'https://scontent.test.fbcdn.net/unrelated.jpg', 'width': 2000, 'height': 2000},
        ]
        result = highest_resolution_listing_images(driver, 'https://www.facebook.com/marketplace/edit/?listing_id=123',
            ['https://scontent.test.fbcdn.net/two.jpg?small=yes', 'https://scontent.test.fbcdn.net/one.jpg?small=yes'])
        self.assertEqual(result, ['https://scontent.test.fbcdn.net/two.jpg?full=yes', 'https://scontent.test.fbcdn.net/one.jpg?full=yes'])
        driver.switch_to.new_window.assert_called_once_with('tab')
        driver.close.assert_called_once()
        driver.switch_to.window.assert_called_once_with('edit-tab')

    def test_model_fallback_does_not_read_the_inventory_container(self):
        html = '''<main><span><span>2021 Honda civic</span>
          <span>MAD34,500 Active · Listed on 10/10</span>
          <span>Listed on Marketplace · 4 clicks on listing</span>
          <span>2019 Ford Transit Van</span><span>MAD23,995</span></span></main>'''
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))
        self.assertEqual(_model_from_visible_listing_heading(self.driver, '2021', 'Honda', False), 'civic')

    def test_opened_dialog_heading_has_priority_over_inventory_titles(self):
        html = '''<h1>2021 Honda Accord</h1><span>2021 Honda Accord</span>
          <div role="dialog"><h1><span>2021 Honda </span><span>civic</span></h1></div>'''
        self.driver.get('data:text/html,' + quote(html))
        self.assertEqual(_model_from_visible_listing_heading(self.driver, '2021', 'Honda', False), 'civic')

    def test_rejects_metadata_only_fallback_and_numeric_internal_model_id(self):
        self.driver.get('data:text/html,' + quote('<span>2021 Honda civic MAD34,500 Active Listed on 10/10</span>'))
        self.assertEqual(_model_from_visible_listing_heading(self.driver, '2021', 'Honda', False), '')
        self.assertEqual(_valid_model_text('237284798', allow_numeric=False), '')
        self.assertEqual(_valid_model_text('911'), '911')
        self.assertEqual(_valid_model_text('F-150 XLT'), 'F-150 XLT')


if __name__ == '__main__':
    unittest.main()
