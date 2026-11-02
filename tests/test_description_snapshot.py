"""Exercise description discovery on real rendered controls, without Facebook."""
import unittest
from urllib.parse import quote

from selenium.webdriver.chrome.options import Options

from automation.chromedriver import build_chrome_driver
from automation.reposting import read_edit_form_snapshot


class DescriptionSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        cls.driver = build_chrome_driver(options)
        cls.addClassCleanup(cls.driver.quit)

    def load(self, html):
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))

    def test_hidden_duplicate_and_unrelated_textarea_do_not_replace_live_description(self):
        self.load('''<main role="main">
            <textarea aria-label="Description" style="display:none">Stale hidden value</textarea>
            <div><span>Description (optional)</span><div><textarea id="live">Requested vehicle details</textarea></div></div>
            <textarea aria-label="Private notes">Unrelated notes</textarea>
            </main>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description'],
                         {'value': 'Requested vehicle details', 'state': 'read'})

    def test_contenteditable_description_wins_over_other_visible_textarea(self):
        self.load('''<main role="main"><textarea aria-label="Notes">Not the description</textarea>
            <span id="description-label">Description (optional)</span>
            <div contenteditable="true" role="textbox" aria-labelledby="description-label">Full vehicle details</div>
            </main>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Full vehicle details')

    def test_cleared_textarea_does_not_read_its_original_text_content(self):
        self.load('<main role="main"><textarea aria-label="Description">Original description</textarea></main>')
        self.driver.execute_script('document.querySelector("textarea").value = "";')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description'], {'value': '', 'state': 'empty'})

    def test_aria_hidden_description_mirror_is_ignored(self):
        self.load('''<main role="main">
            <div aria-hidden="true"><textarea aria-label="Description">Mirror value</textarea></div>
            <textarea aria-label="Description">Actual value</textarea></main>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Actual value')

    def test_unrelated_open_dialog_does_not_hide_the_description_from_reader(self):
        self.load('''<main role="main"><textarea aria-label="Description">Vehicle description</textarea></main>
            <div role="dialog"><input aria-label="Search groups" value="group"></div>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Vehicle description')

    def test_multiple_unlabelled_editors_are_not_guessed_as_description(self):
        self.load('<main role="main"><textarea>First unrelated note</textarea><textarea>Second unrelated note</textarea></main>')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['state'], 'unavailable')

    def test_single_textarea_for_another_named_field_is_not_description(self):
        self.load('<main role="main"><textarea aria-label="Private notes">Private note</textarea></main>')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['state'], 'unavailable')

    def test_single_genuinely_unlabelled_textarea_remains_a_supported_fallback(self):
        self.load('<main role="main"><textarea>Unlabelled vehicle details</textarea></main>')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Unlabelled vehicle details')

    def test_hidden_duplicate_input_does_not_override_live_field(self):
        self.load('''<main role="main"><label style="display:none"><span>Price</span><input value="999"></label>
            <label><span>Price</span><input value="15000"></label>
            <textarea aria-label="Description">Details</textarea></main>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['price']['value'], '15000')

    def test_readonly_description_mirror_does_not_shadow_the_writable_editor(self):
        self.load('''<main role="main"><textarea aria-label="Description" readonly>Stale preview</textarea>
            <div><span>Description</span><textarea>Live writable details</textarea></div></main>''')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description']['value'], 'Live writable details')

    def test_readonly_description_can_still_be_read_when_no_editor_exists(self):
        self.load('<main role="main"><textarea aria-label="Description" readonly>Saved details</textarea></main>')
        self.assertEqual(read_edit_form_snapshot(self.driver)['description'], {'value': 'Saved details', 'state': 'disabled'})


if __name__ == '__main__':
    unittest.main()
