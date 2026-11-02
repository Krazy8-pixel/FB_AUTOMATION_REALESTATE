"""Live-browser regressions for description editing and its form review."""
import unittest
from urllib.parse import quote

from selenium.webdriver.chrome.options import Options

from automation.chromedriver import build_chrome_driver
from automation.posting_description import fill_description, normalize_description, read_description
from automation.posting_review import review_posting_fields


class PostingDescriptionTests(unittest.TestCase):
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

    def fill(self, value):
        return fill_description(self.driver, value, log=self.log, timeout=1, stable_for=0.1)

    def test_controlled_value_tracker_and_blur_preserve_state(self):
        self.load('''<textarea aria-label="Description" id="description"></textarea><button>Next</button>
            <script>
            const el=document.querySelector('textarea');
            const proto=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');
            let tracked='',saved='';
            Object.defineProperty(el,'value',{get(){return proto.get.call(this)},set(value){tracked=value;proto.set.call(this,value)}});
            el.addEventListener('input',()=>{if(el.value!==tracked){saved=el.value;tracked=el.value}});
            el.addEventListener('blur',()=>{el.value=saved});
            window.savedDescription=()=>saved;
            </script>''')
        value = 'Clean car.\nFull service history. العربية 🚗'
        self.assertTrue(self.fill(value))
        self.assertEqual(self.driver.execute_script('return window.savedDescription()'), value)
        self.assertEqual(read_description(self.driver)['value'], value)

    def test_keyboard_fallback_runs_when_blur_clears_first_write(self):
        self.load('''<textarea aria-label="Description"></textarea><button>Next</button>
            <script>let blurs=0;document.querySelector('textarea').addEventListener('blur',event=>{
              if(blurs++===0)event.target.value='';
            });</script>''')
        self.assertTrue(self.fill('This description must survive blur.'))
        methods = [(method, status) for _, method, status, _ in self.events]
        self.assertIn(('native value setter + input/change', 'FAILED'), methods)
        self.assertIn(('keyboard replace', 'SUCCESS'), methods)

    def test_fresh_editor_is_read_after_react_style_node_replacement(self):
        self.load('''<textarea aria-label="Description" id="old"></textarea><button>Next</button>
            <script>document.querySelector('textarea').addEventListener('input',event=>{
              const next=document.createElement('textarea');next.setAttribute('aria-label','Description');
              next.id='fresh';next.value=event.target.value;event.target.replaceWith(next);
            },{once:true});</script>''')
        self.assertTrue(self.fill('Description retained by the replacement editor.'))
        self.assertEqual(self.driver.execute_script('return document.querySelector("textarea").id'), 'fresh')

    def test_contenteditable_and_unrelated_notes_use_same_control_for_review(self):
        self.load('''<main role="main"><textarea aria-label="Private notes">Do not change</textarea>
            <span id="label">Description (optional)</span>
            <div contenteditable="true" role="textbox" aria-labelledby="label"><p>Old text</p></div>
            <button>Next</button></main>''')
        value = 'First line\nSecond line with vehicle details.'
        self.assertTrue(self.fill(value))
        self.assertEqual(normalize_description(read_description(self.driver)['value']), normalize_description(value))
        result = review_posting_fields(self.driver, {'description': value}, lambda *args: False,
                                       stage='before Publish', labels={'description': 'Description'},
                                       log=self.log, settle_for=0.01)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.driver.execute_script('return document.querySelector("textarea").value'), 'Do not change')

    def test_hidden_and_readonly_mirrors_are_not_written_or_reviewed(self):
        self.load('''<textarea aria-label="Description" style="display:none">Hidden old value</textarea>
            <textarea aria-label="Description" readonly>Readonly mirror</textarea>
            <div><span>Description (optional)</span><textarea id="live"></textarea></div><button>Next</button>''')
        value = 'Requested description in the writable editor.'
        self.assertTrue(self.fill(value))
        self.assertEqual(read_description(self.driver)['value'], value)
        self.assertEqual(self.driver.execute_script('return document.querySelector("textarea[readonly]").value'), 'Readonly mirror')

    def test_persistent_clearing_does_not_report_description_success(self):
        self.load('''<textarea aria-label="Description"></textarea><button>Next</button>
            <script>const el=document.querySelector('textarea');
            el.addEventListener('input',()=>{el.value=''});el.addEventListener('blur',()=>{el.value=''});</script>''')
        self.assertFalse(self.fill('Text that the page keeps clearing.'))
        self.assertEqual(read_description(self.driver)['value'], '')
        self.assertTrue(any(method == 'all description methods' and status == 'FAILED'
                            for _, method, status, _ in self.events))

    def test_unrelated_single_named_textarea_is_not_a_description_fallback(self):
        self.load('<textarea aria-label="Private notes">Keep this unchanged</textarea>')
        self.assertFalse(fill_description(self.driver, 'Description text', log=self.log, timeout=0.1, stable_for=0.01))
        self.assertEqual(self.driver.execute_script('return document.querySelector("textarea").value'), 'Keep this unchanged')


if __name__ == '__main__':
    unittest.main()
