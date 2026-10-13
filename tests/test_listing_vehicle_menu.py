import unittest
from unittest.mock import Mock
from urllib.parse import quote
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver
from automation.auto_listing_excel_poster import (_open_vehicle_type_menu, _visible_vehicle_type_choices,
    _select_marketplace_combo, _marketplace_combo_choices, _marketplace_combo_value,
    _marketplace_publish_confirmed)


class PublishConfirmationTests(unittest.TestCase):
    def test_create_page_without_confirmation_is_not_success(self):
        driver = Mock(current_url='https://www.facebook.com/marketplace/create/vehicle')
        driver.execute_script.return_value = False
        self.assertFalse(_marketplace_publish_confirmed(driver))

    def test_explicit_publication_message_confirms_success(self):
        driver = Mock(current_url='https://www.facebook.com/marketplace/create/vehicle')
        driver.execute_script.return_value = True
        self.assertTrue(_marketplace_publish_confirmed(driver))

    def test_published_destinations_confirm_success(self):
        for path in ('/marketplace/you/selling', '/marketplace/item/123/'):
            driver = Mock(current_url='https://www.facebook.com' + path)
            self.assertTrue(_marketplace_publish_confirmed(driver))
            driver.execute_script.assert_not_called()


class VehicleMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = build_chrome_driver(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def load(self, html):
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))

    def fixture(self, handler, popup='hidden', choices=None, label='Vehicle type'):
        choices = choices or '<div role="option" onclick="choose(this)"><span>Car/van</span></div><div role="option">Other</div>'
        self.load('''<label id="combo" role="combobox" tabindex="0" aria-controls="types" aria-expanded="false">
          <span>''' + label + '''</span><span id="value"></span></label>
          <div id="types" role="listbox" ''' + popup + '>' + choices + '''</div>
          <script>
          window.clicks = 0;
          const combo=document.getElementById('combo'), types=document.getElementById('types');
          function openMenu() { types.hidden=false; combo.setAttribute('aria-expanded','true'); }
          function choose(el) { document.getElementById('value').textContent=el.textContent; types.hidden=true;
            combo.setAttribute('aria-expanded','false'); }
          combo.onclick = () => { window.clicks++; ''' + handler + ''' };
          </script>''')

    def test_recovers_when_first_click_is_lost(self):
        self.fixture('if(window.clicks > 1) openMenu();')
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        self.assertEqual(self.driver.execute_script('return window.clicks'), 2)
        choices = _visible_vehicle_type_choices(self.driver)
        self.assertEqual([text for _, text in choices], ['Car/van', 'Other'])
        choices[0][0].click()
        self.assertEqual(self.driver.execute_script("return document.getElementById('value').textContent"), 'Car/van')
        self.assertEqual(_visible_vehicle_type_choices(self.driver), [])

    def test_keeps_already_open_menu_open(self):
        self.fixture('types.hidden=!types.hidden;', popup='')
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        self.assertEqual(self.driver.execute_script('return window.clicks'), 0)
        self.assertEqual(len(_visible_vehicle_type_choices(self.driver)), 2)

    def test_car_prefix_variants_are_not_filtered_out(self):
        for label in ('Car/truck', 'Car/van', 'Car/vehicle', 'CAR / Truck'):
            with self.subTest(label=label):
                self.fixture('openMenu();', choices=f'''
                  <div role="option" onclick="choose(this)">{label}</div>
                  <div role="option">Motorcycle</div><div role="option">Other</div>''')
                _open_vehicle_type_menu(self.driver, timeout=0.2)
                choices = _visible_vehicle_type_choices(self.driver)
                self.assertEqual([text for _, text in choices], [label, 'Motorcycle', 'Other'])
                choices[0][0].click()
                self.assertEqual(_marketplace_combo_value(self.driver, 'Vehicle type'), label)

    def test_car_text_only_option_is_found_beside_role_options(self):
        self.fixture('openMenu();', choices='''
          <div onclick="choose(this)"><span>Car/truck</span></div>
          <div role="option">Motorcycle</div><div role="option">Other</div>''')
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        choices = _visible_vehicle_type_choices(self.driver)
        car = next(el for el, text in choices if text == 'Car/truck')
        car.click()
        self.assertEqual(_marketplace_combo_value(self.driver, 'Vehicle type'), 'Car/truck')

    def test_waits_for_delayed_menu_without_second_toggle(self):
        self.fixture("combo.setAttribute('aria-expanded','true'); setTimeout(openMenu, 150);")
        _open_vehicle_type_menu(self.driver, timeout=0.5)
        self.assertEqual(self.driver.execute_script('return window.clicks'), 1)

    def test_text_only_choices_are_scoped_to_owned_popup(self):
        self.fixture('openMenu();', choices='<div><span>Car/van</span></div><div><span>Other</span></div>')
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend', '<div role=\"listbox\"><div role=\"option\">Boat</div></div><span>Car/van</span>')")
        self.assertEqual(_visible_vehicle_type_choices(self.driver), [])
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        self.assertEqual([text for _, text in _visible_vehicle_type_choices(self.driver)], ['Car/van', 'Other'])

    def test_keyboard_recovers_when_pointer_clicks_do_nothing(self):
        self.fixture('/* ignored pointer events */')
        self.driver.execute_script("document.getElementById('combo').onkeydown = e => { if(e.key === ' ') openMenu(); }")
        _open_vehicle_type_menu(self.driver, timeout=0.2)
        self.assertEqual(len(_visible_vehicle_type_choices(self.driver)), 2)

    def test_missing_choices_report_clear_error_after_retries(self):
        self.fixture('/* no menu */')
        with self.assertRaisesRegex(RuntimeError, 'after four opening attempts'):
            _open_vehicle_type_menu(self.driver, timeout=0.1)
        self.assertEqual(_visible_vehicle_type_choices(self.driver), [])

    def test_body_style_waits_for_menu_and_retains_saloon(self):
        self.fixture('setTimeout(openMenu, 150);', label='Body style', choices='''
          <div role="option" onclick="choose(this)"><span>Saloon</span></div>
          <div role="option" onclick="choose(this)"><span>Other</span></div>''')
        self.assertEqual(_select_marketplace_combo(self.driver, 'Body style', 'Saloon', 'Other', timeout=1), 'Saloon')
        self.assertEqual(_marketplace_combo_value(self.driver, 'Body style'), 'Saloon')

    def test_body_style_uses_other_without_toggling_open_menu(self):
        self.fixture('types.hidden=!types.hidden; combo.setAttribute("aria-expanded",String(!types.hidden));',
          label='Body style', choices='''<div onclick="choose(this)"><span>Coupe</span></div>
          <div onclick="choose(this)"><span>Other</span></div>''')
        self.assertEqual(_select_marketplace_combo(self.driver, 'Body style', 'Saloon', 'Other', timeout=0.2), 'Other')
        self.assertEqual(self.driver.execute_script('return window.clicks'), 1)

    def test_body_style_maps_saloon_to_sedan_when_available(self):
        self.fixture('openMenu();', label='Body style', choices='''
          <div role="option" onclick="choose(this)">Sedan</div>
          <div role="option" onclick="choose(this)">Other</div>''')
        self.assertEqual(_select_marketplace_combo(self.driver, 'Body style', 'Saloon', 'Other', timeout=0.2), 'Sedan')

    def test_failed_car_selection_never_falls_back_to_other(self):
        self.fixture('openMenu();', choices='''<div role="option">Car/van</div>
          <div role="option" onclick="choose(this)">Other</div>''')
        with self.assertRaisesRegex(RuntimeError, 'No matching Vehicle type option retained'):
            _select_marketplace_combo(self.driver, 'Vehicle type', 'Car', 'Other', timeout=0.1)
        self.assertEqual(_marketplace_combo_value(self.driver, 'Vehicle type'), '')

    def test_explicit_other_vehicle_type_is_allowed(self):
        self.fixture('openMenu();', choices='''
          <div role="option" onclick="choose(this)">Other</div>''')
        self.assertEqual(_select_marketplace_combo(self.driver, 'Vehicle type', 'Other', timeout=0.1), 'Other')

    def test_generic_menu_retries_lost_click_and_ignores_other_field_choices(self):
        self.fixture('if(window.clicks > 1) openMenu();', label='Body style', choices='''
          <div role="option" onclick="choose(this)">Other</div>''')
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div role=\"listbox\"><div role=\"option\">Saloon</div></div>')")
        self.assertEqual(_marketplace_combo_choices(self.driver, 'Body style'), [])
        self.assertEqual(_select_marketplace_combo(self.driver, 'Body style', 'Saloon', 'Other', timeout=0.2), 'Other')
        self.assertEqual(self.driver.execute_script('return window.clicks'), 2)


if __name__ == '__main__':
    unittest.main()
