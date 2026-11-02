"""Successful posting durations render consistently before and after refresh."""
import json
from pathlib import Path
import unittest
from urllib.parse import quote

from bs4 import BeautifulSoup
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from automation.chromedriver import build_chrome_driver


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Environment(
    loader=ChoiceLoader([
        DictLoader({'base.html': '<!doctype html><html><body>{% block content %}{% endblock %}{% block extra_js %}{% endblock %}</body></html>'}),
        FileSystemLoader(str(ROOT / 'web/templates')),
    ]),
    autoescape=select_autoescape(['html']),
)
TEMPLATES.globals['url_for'] = lambda endpoint, **values: '/' + endpoint


def result(status='posted', duration='1m 48s', title='Vehicle'):
    item = {
        'job_id': title, 'title': title, 'account': 'test-account', 'source': 'Manual / Excel',
        'status': status, 'error': '', 'remaining_seconds': 0,
        'time_label': 'Posted' if status == 'posted' else 'Scheduled',
        'time_iso': '2026-10-25T22:00:00+05:00', 'time_clock': '10:00:00 PM',
        'time_date': '25 Oct 2026', 'time_zone': 'PKT', 'timestamp': '2026-10-25 22:00:00',
    }
    if duration is not None:
        item['posting_duration_text'] = duration
    return item


def render(template, items):
    return TEMPLATES.get_template(template).render(
        active_account='test-account', scheduled_items=items,
        pending_count=0, posted_count=0, next_run_label='-',
        repost_items=[], repost_history=items, running_count=0,
    )


class PostingTimeTemplateTests(unittest.TestCase):
    def test_initial_posting_rows_show_duration_only_after_success(self):
        items = [result('posted'), result('failed', '9m 9s'), result('pending', '8m 8s'),
                 result('cancelled', '7m 7s'), result('posting', '6m 6s'),
                 result('posted', None), result('posted', '0s')]
        soup = BeautifulSoup(render('post_schedular.html', items), 'html.parser')
        rows = soup.select('#schedular-rows tr')
        self.assertEqual([cell.get_text(strip=True) for cell in soup.select('.posting-duration')],
                         ['1m 48s', '-', '-', '-', '-', '-', '0s'])
        self.assertTrue(all(len(row.find_all('td', recursive=False)) == 7 for row in rows))
        self.assertEqual(len(soup.select('thead th')), 7)
        self.assertIn('including scheduled delays and queue time', soup.select('thead th')[4]['title'])

    def test_initial_empty_posting_table_spans_all_columns(self):
        soup = BeautifulSoup(render('post_schedular.html', []), 'html.parser')
        self.assertEqual(soup.select_one('#schedular-rows .empty')['colspan'], '7')

    def test_initial_repost_history_hides_failed_and_legacy_durations(self):
        items = [result('reposted'), result('failed', '9m 9s'), result('reposted', None)]
        soup = BeautifulSoup(render('reposting.html', items), 'html.parser')
        self.assertEqual([cell.get_text(strip=True) for cell in soup.select('.repost-duration')],
                         ['1m 48s', '-', '-'])
        self.assertTrue(all(len(row.find_all('td', recursive=False)) == 5
                            for row in soup.select('#repost-history-rows tr')))


class PostingTimeRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        cls.driver = build_chrome_driver(options)
        cls.addClassCleanup(cls.driver.quit)

    def load(self, template, initial, refreshed):
        script = '''<script>
            window.mockRows=ROWS;
            window.setTimeout=()=>0;
            window.confirm=()=>false;
            window.appFetch=async()=>({ok:true,json:async()=>({
              ok:true,pending_count:0,posted_count:0,running_count:0,next_run_label:'-',
              items:POSTING?window.mockRows:[],history:POSTING?[]:window.mockRows
            })});
        </script>'''.replace('ROWS', json.dumps(refreshed)).replace('POSTING', 'true' if template == 'post_schedular.html' else 'false')
        html = render(template, initial).replace('<body>', '<body>' + script, 1)
        self.driver.get('data:text/html;charset=utf-8,' + quote(html))

    def cells(self, css):
        return self.driver.execute_script('return [...document.querySelectorAll(arguments[0])].map(el=>el.textContent.trim());', css)

    def test_refreshed_posting_rows_apply_success_gate_and_keep_columns(self):
        items = [result('posted'), result('failed', '9m 9s'), result('pending', '8m 8s'),
                 result('posted', None), result('posted', '0s')]
        self.load('post_schedular.html', [], items)
        WebDriverWait(self.driver, 3).until(lambda driver: len(self.cells('.posting-duration')) == len(items))
        self.assertEqual(self.cells('.posting-duration'), ['1m 48s', '-', '-', '-', '0s'])
        self.assertEqual(self.driver.execute_script('return [...document.querySelectorAll("#schedular-rows tr")].map(row=>row.children.length);'), [7] * len(items))

    def test_failed_refresh_removes_previously_shown_duration(self):
        self.load('post_schedular.html', [result('posted')], [result('failed', '9m 9s')])
        WebDriverWait(self.driver, 3).until(lambda driver: self.cells('.posting-duration') == ['-'])
        self.assertEqual(self.cells('.posting-duration'), ['-'])

    def test_empty_refresh_spans_all_columns(self):
        self.load('post_schedular.html', [result('posted')], [])
        WebDriverWait(self.driver, 3).until(lambda driver: bool(driver.execute_script('return document.querySelector("#schedular-rows td.empty");')))
        self.assertEqual(self.driver.execute_script('return document.querySelector("#schedular-rows td.empty").colSpan;'), 7)

    def test_refreshed_duration_text_is_escaped(self):
        duration = '<img src=x onerror=alert(1)>'
        self.load('post_schedular.html', [], [result('posted', duration)])
        WebDriverWait(self.driver, 3).until(lambda driver: self.cells('.posting-duration') == [duration])
        self.assertEqual(self.driver.execute_script('return document.querySelectorAll(".posting-duration img").length;'), 0)

    def test_repost_refresh_only_shows_successful_duration(self):
        items = [result('reposted'), result('failed', '9m 9s'), result('reposted', None)]
        self.load('reposting.html', [], items)
        WebDriverWait(self.driver, 3).until(lambda driver: len(self.cells('.repost-duration')) == len(items))
        self.assertEqual(self.cells('.repost-duration'), ['1m 48s', '-', '-'])
        self.assertEqual(self.driver.execute_script('return [...document.querySelectorAll("#repost-history-rows tr")].map(row=>row.children.length);'), [5] * len(items))


if __name__ == '__main__':
    unittest.main()
