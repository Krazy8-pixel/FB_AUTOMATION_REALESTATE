import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver
from automation.delete_candidates import extract_delete_candidates
from automation import delete_listing_import as importer


class DeleteDialogTests(unittest.TestCase):
    def test_final_five_second_pause_is_preserved_and_element_waits_are_25(self):
        driver, menu, confirm = MagicMock(), MagicMock(), MagicMock()
        with patch.object(importer, 'WebDriverWait') as wait, \
             patch.object(importer, '_click_element_safely', return_value=True) as click, \
             patch.object(importer.time, 'sleep') as sleep:
            wait.return_value.until.side_effect = [menu, confirm, True]
            importer._complete_facebook_delete_dialog(driver)
        self.assertEqual([call.args[1] for call in wait.call_args_list], [25, 25, 25])
        self.assertEqual([call.args[1] for call in click.call_args_list], [menu, confirm])
        sleep.assert_called_once_with(5)

    def test_new_scanner_never_looks_up_image_elements(self):
        driver = MagicMock()
        driver.execute_script.return_value = []
        def find(by, selector):
            self.assertNotIn('img', selector)
            return []
        driver.find_elements.side_effect = find
        self.assertEqual(extract_delete_candidates(driver), [])
        script = driver.execute_script.call_args.args[0]
        self.assertNotIn("querySelector('img')", script)
        self.assertNotIn('currentSrc', script)


class DeleteCandidateBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = build_chrome_driver(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def test_same_text_rows_are_found_even_without_any_images(self):
        self.driver.get('data:text/html,' + quote('''<main role="main">
          <div><input aria-label="Select Listing" type="checkbox"><div role="button" aria-label="2020 Ford Focus">2020 Ford Focus $100</div>
          <a href="https://www.facebook.com/marketplace/item/123/">View listing</a><div role="button" aria-label="More options for 2020 Ford Focus"></div></div>
          <div><input aria-label="Select Listing" type="checkbox"><div role="button" aria-label="2020 Ford Focus">2020 Ford Focus $200</div>
          <a href="https://www.facebook.com/marketplace/item/456/">View listing</a><div role="button" aria-label="More options for 2020 Ford Focus"></div></div>
          </main>'''))
        candidates = extract_delete_candidates(self.driver)
        rows = [item for item in candidates if item.get('source') == 'full_row_dom']
        self.assertEqual(len(rows), 2)
        self.assertEqual([item['href'] for item in rows], [
            'https://www.facebook.com/marketplace/item/123/', 'https://www.facebook.com/marketplace/item/456/'])
        self.assertTrue(all(item['images'] == [] for item in candidates))
        self.assertIn('$100', rows[0]['text'])
        self.assertIn('$200', rows[1]['text'])


if __name__ == '__main__':
    unittest.main()
