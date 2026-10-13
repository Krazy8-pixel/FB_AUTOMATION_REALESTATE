"""Inspect the live vehicle-type menu without uploading, submitting or publishing."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from automation.delete_listing_import import _open_prelogged_facebook
from automation.auto_listing_excel_poster import (_combobox_label_xpath, _open_vehicle_type_menu,
    _visible_vehicle_type_choices, _open_marketplace_combo, _marketplace_combo_choices, _select_marketplace_combo)


def main():
    driver = _open_prelogged_facebook(sys.argv[1] if len(sys.argv) > 1 else 'Main', force_headless=True)
    try:
        driver.get('https://www.facebook.com/marketplace/create/vehicle')
        combo = WebDriverWait(driver, 45).until(EC.element_to_be_clickable(
            (By.XPATH, _combobox_label_xpath('Vehicle type'))))
        _open_vehicle_type_menu(driver)
        snapshot = driver.execute_script("""
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          return {combo: arguments[0].outerHTML,
            popups: [...document.querySelectorAll('[role="listbox"],[role="menu"],[role="option"],[role="menuitem"],[role="radio"]')].filter(visible).map(el=>el.outerHTML),
            choices: [...document.querySelectorAll('span')].filter(el=>visible(el) && /^(Car\\/van|Car|Other|Motorcycle|Boat|Truck)$/i.test(el.textContent.trim())).map(el=>el.parentElement.outerHTML)};
        """, combo)
        root = Path(__file__).resolve().parents[1] / 'data/listing_diagnostics'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'vehicle_type_menu.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        driver.save_screenshot(str(root / 'vehicle_type_menu.png'))
        choices = _visible_vehicle_type_choices(driver)
        print('Visible vehicle choices:', [text for _, text in choices])
        car = next(el for el, text in choices if text == 'Car/van')
        car.click()
        WebDriverWait(driver, 10).until(lambda d: not _visible_vehicle_type_choices(d))
        combo = driver.find_element(By.XPATH, _combobox_label_xpath('Vehicle type'))
        assert 'Car/van' in combo.text, 'Facebook did not retain the vehicle type'
        print('Live menu selection passed: Car/van retained. Nothing uploaded or published.')
        body = WebDriverWait(driver, 20).until(EC.element_to_be_clickable(
            (By.XPATH, _combobox_label_xpath('Body style'))))
        _open_marketplace_combo(driver, 'Body style')
        body_snapshot = driver.execute_script("""
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          return {combo: arguments[0].outerHTML, popups: [...document.querySelectorAll('[role="listbox"]')].filter(visible).map(el=>el.outerHTML)};
        """, body)
        (root / 'body_style_menu.json').write_text(json.dumps(body_snapshot, indent=2), encoding='utf-8')
        print('Live body-style choices:', [text for _, text in _marketplace_combo_choices(driver, 'Body style')])
        print('Body style selected:', _select_marketplace_combo(driver, 'Body style', 'Saloon', 'Other'))
        print('Body style fallback selected:', _select_marketplace_combo(driver, 'Body style', 'Unavailable body style', 'Other'))
        print('Nothing uploaded, published or deleted.')
    finally:
        driver.quit()


if __name__ == '__main__':
    main()
