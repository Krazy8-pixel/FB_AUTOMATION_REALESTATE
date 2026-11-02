"""Real-Chrome regression cases for controlled posting input replacement."""
import time
import unittest
from urllib.parse import quote

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from automation.chromedriver import build_chrome_driver
from automation.auto_listing_excel_poster import _input_for_label_xpath
from automation.posting_description import fill_description
from automation.posting_inputs import fill_posting_input
from automation.posting_numbers import parse_numeric_value
from automation.posting_review import normalize_field_value, review_posting_fields
from automation.reposting import read_edit_form_snapshot


class PostingInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        cls.driver = build_chrome_driver(options)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        self.events = []

    def load(self, html):
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))

    def log(self, field, method, status, **details):
        self.events.append((field, method, status, details))

    def fill(self, key, value):
        label = key.title()
        locator = (By.CSS_SELECTOR, f'input[aria-label="{label}"]')
        return fill_posting_input(self.driver, key, label, value, locator,
                                  log=self.log, timeout=1, stable_for=0.1)

    def actual(self):
        return self.driver.execute_script('return document.querySelector("input:not([type=hidden])").value')

    def test_currency_price_is_written_as_numeric_amount_only(self):
        self.load(r'''<input aria-label="Price" value="111,000 د.م."><button>Next</button>
            <script>window.inputValues=[];
            const el=document.querySelector('input');
            el.addEventListener('input',event=>{window.inputValues.push(event.target.value)});
            el.addEventListener('blur',()=>{const n=el.value.replace(/\D/g,'');
              if(n)el.value=Number(n).toLocaleString('en-US')+' د.م.'});</script>''')
        self.assertTrue(self.fill('price', 'MAD567,000'))
        self.assertEqual(self.actual(), '567,000 د.م.')
        values = self.driver.execute_script('return window.inputValues')
        self.assertFalse(any('MAD' in value for value in values))
        self.assertEqual(parse_numeric_value(self.actual()), parse_numeric_value('567000'))

    def test_restored_old_value_is_never_followed_by_appended_typing(self):
        self.load(r'''<input aria-label="Price" value="111,000 د.م."><button>Next</button>
            <script>const el=document.querySelector('input'); window.nonemptyInputs=[];
            el.addEventListener('input',event=>{
              if(!el.value){el.value='111,000 د.م.';return}
              window.nonemptyInputs.push(el.value);
            });
            el.addEventListener('blur',()=>{const n=el.value.replace(/\D/g,'');
              if(n)el.value=Number(n).toLocaleString('en-US')+' د.م.'});</script>''')
        self.assertTrue(self.fill('price', 'MAD567,000'))
        self.assertEqual(self.actual(), '567,000 د.م.')
        self.assertEqual(self.driver.execute_script('return window.nonemptyInputs'), ['567000'])
        methods = [(method, status) for _, method, status, _ in self.events]
        self.assertIn(('keyboard select-all, clear, and replace', 'FAILED'), methods)
        self.assertIn(('atomic native value setter + input/change fallback', 'SUCCESS'), methods)

    def test_fresh_input_after_clear_replaces_expected_text(self):
        self.load('''<input aria-label="Model" id="old" value="Old model"><button>Next</button>
            <script>document.querySelector('input').addEventListener('input',event=>{
              if(event.target.value===''){
                const next=document.createElement('input'); next.setAttribute('aria-label','Model');
                next.id='fresh';event.target.replaceWith(next);
              }
            });</script>''')
        self.assertTrue(self.fill('model', 'Civic Sport'))
        self.assertEqual(self.actual(), 'Civic Sport')
        self.assertEqual(self.driver.execute_script('return document.querySelector("input").id'), 'fresh')

    def test_input_replaced_during_typing_uses_fresh_atomic_fallback(self):
        self.load('''<input aria-label="Model" id="old" value="Old"><button>Next</button>
            <script>document.querySelector('input').addEventListener('input',event=>{
              if(event.target.value){const next=document.createElement('input');
                next.setAttribute('aria-label','Model');next.id='fresh';next.value=event.target.value;
                event.target.replaceWith(next)}
            });</script>''')
        self.assertTrue(self.fill('model', 'Civic Sport'))
        self.assertEqual(self.actual(), 'Civic Sport')
        self.assertTrue(any(method == 'atomic native value setter + input/change fallback' and status == 'SUCCESS'
                            for _, method, status, _ in self.events))

    def test_empty_model_is_filled_without_losing_punctuation(self):
        self.load('<input aria-label="Model"><button>Next</button>')
        self.assertTrue(self.fill('model', 'MX-5 GT'))
        self.assertEqual(self.actual(), 'MX-5 GT')

    def test_currency_only_blank_mask_does_not_count_as_old_amount(self):
        self.load(r'''<input aria-label="Price" value="111,000 د.م."><button>Next</button>
            <script>const el=document.querySelector('input');
            el.addEventListener('input',()=>{if(el.value==='')el.value='د.م.'});
            el.addEventListener('blur',()=>{const n=el.value.replace(/\D/g,'');
              if(n)el.value=Number(n).toLocaleString('en-US')+' د.م.'});</script>''')
        self.assertTrue(self.fill('price', 'MAD567,000'))
        self.assertEqual(parse_numeric_value(self.actual()), parse_numeric_value('567000'))

    def test_hidden_mirror_is_not_written(self):
        self.load('''<input type="hidden" aria-label="Price" value="999">
            <input aria-label="Price" value="111"><button>Next</button>''')
        self.assertTrue(self.fill('price', 'MAD567,000'))
        self.assertEqual(self.driver.execute_script('return document.querySelector("input[type=hidden]").value'), '999')
        self.assertEqual(self.actual(), '567000')

    def test_decimal_amount_is_not_truncated_to_whole_number(self):
        self.load('<input aria-label="Price" value="111"><button>Next</button>')
        self.assertTrue(self.fill('price', 'MAD567,000.50'))
        self.assertEqual(parse_numeric_value(self.actual()), parse_numeric_value('567000.50'))
        self.assertNotEqual(parse_numeric_value(self.actual()), parse_numeric_value('567000'))

    def test_numeric_zero_is_written_and_verified(self):
        self.load('<input aria-label="Price" value="111"><button>Next</button>')
        self.assertTrue(self.fill('price', 0))
        self.assertEqual(self.actual(), '0')

    def test_persistent_restoration_fails_without_waiting_for_known_wrong_value(self):
        self.load('''<input aria-label="Price" value="111,000 د.م."><button>Next</button>
            <script>const el=document.querySelector('input');const reset=()=>{el.value='111,000 د.م.'};
            el.addEventListener('input',reset);el.addEventListener('change',reset);el.addEventListener('blur',reset);</script>''')
        began = time.monotonic()
        self.assertFalse(self.fill('price', 'MAD567,000'))
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(self.actual(), '111,000 د.م.')
        self.assertTrue(any(method == 'all replacement methods' and status == 'FAILED'
                            for _, method, status, _ in self.events))

    def review_real_form(self, supplied):
        labels = {'price': 'Price', 'model': 'Model', 'description': 'Description'}
        refilled = []
        verified = set()

        def refill(key, label, value):
            refilled.append(key)
            if key == 'description':
                return fill_description(self.driver, value, log=self.log, timeout=1, stable_for=0.1)
            return fill_posting_input(self.driver, key, label, value,
                                      (By.XPATH, _input_for_label_xpath(label)),
                                      log=self.log, timeout=1, stable_for=0.1)

        # Use the default live snapshot reader, including the real poster's
        # label/span XPath for every input repair. No snapshot is fabricated.
        result = review_posting_fields(self.driver, supplied, refill,
                                       stage='before Publish', labels=labels,
                                       verified_fields=verified, log=self.log,
                                       timeout=1, settle_for=0.01)
        snapshot = read_edit_form_snapshot(self.driver)
        self.assertTrue(result.ok, result.message)
        for key, value in supplied.items():
            self.assertEqual(normalize_field_value(key, snapshot[key]['value']),
                             normalize_field_value(key, value), key)
        self.assertTrue(set(supplied).issubset(verified))
        return snapshot, refilled

    def test_real_form_review_keeps_correct_currency_price_and_repairs_model_description(self):
        self.load('''<main role="main">
            <label><span>Price</span><input id="price" value="567,000 د.م."></label>
            <label><span>Model</span><input value="Wrong model"></label>
            <label><span>Description</span><textarea></textarea></label>
            <button>Next</button></main>
            <script>window.priceWrites=[];document.getElementById('price').addEventListener('input',event=>{
              window.priceWrites.push(event.target.value);
            });</script>''')
        supplied = {'price': 'MAD567,000', 'model': 'civic', 'description': 'very nice'}
        snapshot, refilled = self.review_real_form(supplied)
        self.assertNotIn('price', refilled)
        self.assertEqual(set(refilled), {'model', 'description'})
        self.assertEqual(self.driver.execute_script('return window.priceWrites'), [])
        self.assertEqual(snapshot['price']['value'], '567,000 د.م.')
        self.assertEqual(snapshot['model']['value'], 'civic')
        self.assertEqual(snapshot['description']['value'], 'very nice')

    def test_real_form_review_replaces_corrupted_price_without_appending_currency(self):
        self.load(r'''<main role="main">
            <label><span>Price</span><input id="price" value="567,000,567 د.م."></label>
            <label><span>Model</span><input value="civic"></label>
            <label><span>Description</span><textarea>very nice</textarea></label>
            <button>Next</button></main>
            <script>window.nonemptyPriceWrites=[];
            const price=document.getElementById('price');price.addEventListener('input',event=>{
              if(!price.value){price.value='567,000,567 د.م.';return}
              window.nonemptyPriceWrites.push(price.value);
            });
            price.addEventListener('blur',()=>{const digits=price.value.replace(/\D/g,'');
              if(digits)price.value=Number(digits).toLocaleString('en-US')+' د.م.'});</script>''')
        supplied = {'price': 'MAD567,000', 'model': 'civic', 'description': 'very nice'}
        snapshot, refilled = self.review_real_form(supplied)
        self.assertEqual(refilled, ['price'])
        self.assertEqual(snapshot['price']['value'], '567,000 د.م.')
        writes = self.driver.execute_script('return window.nonemptyPriceWrites')
        self.assertEqual(writes, ['567000'])
        self.assertFalse(any('MAD' in value or '567000567' in value for value in writes))


if __name__ == '__main__':
    unittest.main()
