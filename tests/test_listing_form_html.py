import unittest
from unittest.mock import MagicMock
from urllib.parse import quote
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver
from automation.reposting import read_edit_form_snapshot, wait_for_edit_form_ready, highest_resolution_listing_images, _find_edit_listing_link, _model_from_visible_listing_heading, _valid_model_text, _item_edit_details, _safe_image_urls


class ListingFormTests(unittest.TestCase):
    def test_vehicle_condition_accepts_short_condition_label(self):
        self.driver.get('data:text/html,' + quote('''<main role="main">
            <label>Price<input value="15500"></label>
            <label role="combobox"><span>Condition</span><span>Good</span></label>
            </main>'''))
        snapshot = read_edit_form_snapshot(self.driver)
        self.assertEqual(snapshot['vehicle_condition']['value'], 'Good')
        self.assertEqual(snapshot['vehicle_condition']['state'], 'read')

    def test_long_scrolling_description_colors_vin_and_trim_are_read_in_full(self):
        from automation.visual_listing_reader import capture_description_scroll
        from html import escape
        import tempfile
        description = '2015 Lexus RC 350 3.5L V6\n\n' + '\n'.join(f'Feature line {i}' for i in range(60)) + '\nPrice 18,500 NON-NEGOTIABLE'
        html = f'''<div role="dialog"><main role="main"><h1>Edit listing</h1>
            <label><span>VIN</span><input value="TESTVIN1234567890"></label>
            <label role="combobox"><span>Vehicle type</span><span>Car/Truck</span></label>
            <label><span>Model</span><input value="985374995825728"></label>
            <label><span>Trim</span><input value="F Sport"></label>
            <label><span>Price</span><input value="$18,500"></label>
            <label role="combobox"><span>Exterior color</span><span>White</span></label>
            <label role="combobox"><span>Interior color</span><span>Black</span></label>
            <label><span>Description</span><textarea style="width:400px;height:80px">{escape(description)}</textarea></label>
            </main></div>'''
        self.driver.get('data:text/html,' + quote(html))
        values = wait_for_edit_form_ready(self.driver, timeout=5)
        self.assertEqual(values['description']['value'], description)
        self.assertEqual(values['exterior_color']['value'], 'White')
        self.assertEqual(values['interior_color']['value'], 'Black')
        self.assertEqual(values['vin']['value'], 'TESTVIN1234567890')
        self.assertEqual(values['trim']['value'], 'F Sport')
        with tempfile.TemporaryDirectory() as folder:
            captures = capture_description_scroll(self.driver, folder)
        self.assertGreater(len(captures), 1)
        self.assertEqual(self.driver.execute_script('return document.querySelector("textarea").scrollTop'), 0)

    def test_vehicle_layout_takes_precedence_over_item_labels(self):
        snapshot = {key: {'value': value, 'state': 'read'} for key, value in {
            'title': '2020 Ford Focus', 'category': 'Vehicles', 'vehicle_type': 'Car', 'mileage': '50000'}.items()}
        self.assertIsNone(_item_edit_details(snapshot, {}, []))

    def test_item_style_dialog_reads_textarea_and_all_attached_photos(self):
        # Same semantic structure as the supplied Facebook dialog, anonymized.
        photos = ''.join(f'<div><div role="button" aria-label="Remove photo {i} of 10"></div>'
            f'<img alt="Listing photo: No photo description available." src="https://scontent.test.fbcdn.net/{i}.jpg"></div>'
            for i in range(1, 11))
        description = 'Project car.\n\nNeeds a frame and front end.'
        html = f'''<div role="dialog"><div role="main"><h1>Edit listing</h1>
          <div role="form" aria-label="Marketplace">{photos}
          <label><input type="file" accept="image/*" multiple></label>
          <label><div><span>Title</span><input value="1971 Chevrolet El camino"></div></label>
          <label><div><span>Price</span><input value="$9,800"></div></label>
          <label role="combobox" aria-labelledby="category"><div><span id="category">Category</span><div><span>&nbsp;</span></div></div></label>
          <label role="combobox" aria-labelledby="condition"><span id="condition">Condition</span><div><span>&nbsp;</span></div></label>
          <label><div><span>Description</span><div aria-hidden="true">Mirror must not be duplicated</div><textarea>{description}</textarea></div></label>
          <label><span>SKU</span><input value=""></label></div></div>
          <div role="region" aria-label="Preview"><img src="https://scontent.test.fbcdn.net/1.jpg"><img src="https://scontent.test.fbcdn.net/avatar.jpg"></div></div>'''
        self.driver.get('data:text/html,' + quote(html))
        snapshot = wait_for_edit_form_ready(self.driver, timeout=5)
        photos = _safe_image_urls(self.driver)
        post = _item_edit_details(snapshot, {'year': '1971', 'make': 'Chevrolet', 'model': 'El camino'}, photos)
        self.assertEqual(post['title'], '1971 Chevrolet El camino')
        self.assertEqual(post['price'], '$9,800')
        self.assertEqual(post['description'], description)
        for field in ('category', 'item_condition', 'sku'):
            self.assertEqual(post['field_states'][field], 'empty')
        self.assertEqual(post['field_states']['mileage'], 'unavailable')
        self.assertEqual(post['mileage'], '')
        self.assertEqual(photos, [f'https://scontent.test.fbcdn.net/{i}.jpg' for i in range(1, 11)])

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
