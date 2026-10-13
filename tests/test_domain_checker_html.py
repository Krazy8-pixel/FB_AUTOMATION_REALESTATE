"""Offline browser checks against the supplied GoDaddy HTML (no site requests)."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import zipfile
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'checker_html', Path(__file__).resolve().parents[1] / 'automation' / 'bbc.py'
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
from playwright.sync_api import sync_playwright

FIXTURES = Path(__file__).parent / 'fixtures' / 'domain_checker'
AVAILABLE = '''<div data-cy="availcard">
<span data-cy="availableCard-tag">GREAT NAME</span>
<h2 data-cy="two-tone-domain"><div>aishjksd<span>.com</span></div></h2>
<span data-cy="pricing-main-price">₨1</span>
<del data-cy="pricing-strikethrough-price">₨4,999</del>
<button data-cy="availableCard-add-to-cart-button">Make It Yours</button>
</div>'''


class HtmlResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(
            executable_path=checker.chrome_executable(), headless=True
        )
        cls.page = cls.browser.new_page()
        cls.page.route('**/*', lambda route: route.abort())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def test_supplied_cards_and_exact_domain_matching(self):
        cases = [
            ('taken_alternative.html', 'takeai.com', ('Taken', None)),
            ('taken_broker.html', 'petai.com', ('Taken', None)),
            ('listed.html', 'asklyai.com', ('Listed', '₨2,107,334')),
        ]
        for filename, domain, expected in cases:
            with self.subTest(filename=filename):
                self.page.set_content((FIXTURES / filename).read_text(encoding='utf-8'))
                self.assertEqual(checker.extract_result(self.page, domain), expected)
                self.assertIsNone(checker.extract_result(self.page, 'unrelated.com'))

    def test_available_layouts_and_price(self):
        for layout in ('availcard', 'availableCard'):
            with self.subTest(layout=layout):
                self.page.set_content(AVAILABLE.replace('"availcard"', f'"{layout}"'))
                self.assertEqual(checker.extract_result(self.page, 'aishjksd.com'),
                                 ('Unregistered/Available', '₨1'))
                self.assertIsNone(checker.extract_result(self.page, 'aishjksd.net'))

    def test_taken_beats_available_and_ignores_hidden_cards(self):
        taken = (FIXTURES / 'taken_alternative.html').read_text(encoding='utf-8')
        available = AVAILABLE.replace('aishjksd', 'takeai')
        self.page.set_content(available + taken)
        self.assertEqual(checker.extract_result(self.page, 'takeai.com'), ('Taken', None))
        self.page.set_content(available + '<div style="display:none">' + taken + '</div>')
        self.assertEqual(checker.extract_result(self.page, 'takeai.com'),
                         ('Unregistered/Available', '₨1'))

    def test_screenshot_captures_exact_taken_card(self):
        self.page.set_content((FIXTURES / 'taken_alternative.html').read_text(encoding='utf-8'))
        result = checker.find_result_card(self.page, 'takeai.com')
        self.assertEqual(result[2].get_attribute('data-cy'), 'flattened-taken-exact')
        image = checker.capture_result_screenshot(self.page, 'takeai.com')
        self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertEqual(image, result[2].screenshot(type='png', animations='disabled'))

    def test_clickable_full_size_images_survive_reopen_and_append(self):
        self.page.set_content(AVAILABLE)
        image = checker.capture_result_screenshot(self.page, 'aishjksd.com')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'domains.xlsx'
            workbook, sheet = checker.open_results(path)
            checker.append_result(workbook, sheet, path, 'aishjksd.com', 'Unregistered/Available', '₨1', image)
            workbook.close()
            workbook, sheet = checker.open_results(path)
            self.assertEqual(sheet['D2'].value, 'View screenshot')
            self.assertEqual(sheet['D2'].hyperlink.location, "'Screenshot 1'!A1")
            self.assertIsNone(sheet['D2'].comment)
            self.assertIsNone(sheet.row_dimensions[2].height)
            checker.append_result(workbook, sheet, path, 'example.com', 'Taken', None, image)
            workbook.close()
            workbook, sheet = checker.open_results(path)
            self.assertEqual(workbook.sheetnames, ['Domains', 'Screenshot 1', 'Screenshot 2'])
            self.assertEqual(sheet['D3'].hyperlink.location, "'Screenshot 2'!A1")
            self.assertEqual(sheet.auto_filter.ref, 'A1:D3')
            checker.save_atomic(workbook, path)
            checker.save_atomic(workbook, path)
            import struct
            width, height = struct.unpack('>II', image[16:24])
            for name in ('Screenshot 1', 'Screenshot 2'):
                image_sheet = workbook[name]
                self.assertEqual(len(image_sheet._images), 1)
                picture = image_sheet._images[0]
                max_width, max_height = checker.screenshot_display_bounds()
                self.assertLessEqual(picture.width, max_width)
                self.assertLessEqual(picture.height, max_height)
                self.assertAlmostEqual(picture.width / picture.height, width / height)
                self.assertTrue(image_sheet['A1'].hyperlink.location.startswith("'Domains'!D"))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(list(checker.note_drawings(archive)), [])
                media = [name for name in archive.namelist() if name.startswith('xl/media/')]
                self.assertEqual(len(media), 2)
                for name in media:
                    self.assertEqual(archive.read(name), image)
            workbook.close()

    def test_old_three_column_workbook_and_capture_failure(self):
        from openpyxl import Workbook
        from playwright.sync_api import Error
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'domains.xlsx'
            old = Workbook()
            old.active.title = 'Domains'
            old.active.append(checker.HEADERS)
            old.active.append(('old.com', 'Taken', None))
            old.save(path)
            old.close()
            workbook, sheet = checker.open_results(path)
            self.assertEqual(sheet['D1'].value, 'Screenshot')
            with patch.object(checker, 'find_result_card', side_effect=Error('Capture failed')):
                image = checker.capture_result_screenshot(self.page, 'example.com')
            self.assertIsNone(image)
            checker.append_result(workbook, sheet, path, 'example.com', 'Taken', None, image)
            workbook.close()
            workbook, sheet = checker.open_results(path)
            self.assertEqual(sheet['A2'].value, 'old.com')
            self.assertEqual(sheet['D3'].value, 'Not captured')
            self.assertEqual(checker.load_screenshot_notes(path), {})
            workbook.close()


if __name__ == '__main__':
    unittest.main()
