from pathlib import Path
import unittest
from urllib.parse import quote
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver


class PostingClickTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = build_chrome_driver(options)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        script = (Path(__file__).resolve().parents[1] / 'web/static/js/posting_timing.js').read_text()
        html = '''<form id="post" data-posting-timing><input name="title" required value="Car">
        <button id="submit">Post</button><button id="save" type="button">Save</button></form>
        <button id="open" type="button">Choose accounts</button>
        <button id="confirm" type="button" data-posting-timing-form="post">Post to accounts</button>
        <script>window.now=1000000; Date.now=()=>window.now;</script>'''
        html += '<script>' + script + '''
        document.querySelector('form').addEventListener('submit', e=>e.preventDefault());
        </script>'''
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))

    def js(self, script):
        return self.driver.execute_script(script)

    def test_final_click_captured_before_handler_and_in_form_data(self):
        self.js("document.querySelector('#submit').addEventListener('click',()=>window.now+=100); document.querySelector('#submit').click()")
        self.assertEqual(self.js("return new FormData(document.querySelector('form')).get('posting_requested_at_ms')"), '1000000')

    def test_modal_open_and_save_do_not_start_timer(self):
        self.js("document.querySelector('#open').click(); document.querySelector('#save').click()")
        self.assertIsNone(self.js("return document.querySelector('[name=posting_requested_at_ms]')"))
        self.js("document.querySelector('#confirm').click()")
        self.assertEqual(self.js("return new FormData(document.querySelector('form')).get('posting_requested_at_ms')"), '1000000')

    def test_submit_without_click_and_retry_get_fresh_origin(self):
        self.js("document.querySelector('form').requestSubmit()")
        self.assertEqual(self.js("return window.postingTiming.forSubmit(document.querySelector('form'))"), 1000000)
        self.js("window.now+=5000; document.querySelector('#submit').click()")
        self.assertEqual(self.js("return window.postingTiming.forSubmit(document.querySelector('form'))"), 1005000)


if __name__ == '__main__':
    unittest.main()
