"""Standalone domain checker. Setup: pip install playwright openpyxl
Uses installed Google Chrome in a separate, minimized persistent profile.
Also install prompt_toolkit and Pillow. Paste your entire list, then press Enter once to start.
"""
import argparse
from contextlib import contextmanager
import ctypes
import importlib.util
from io import BytesIO
import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import sys
import zipfile
from xml.etree import ElementTree as ET
from urllib.parse import urlsplit

HEADERS = ('Domain name', 'Status', 'Price')
PENDING_PATH = Path(__file__).resolve().with_name('domain_checker_pending.json')
PROFILE_PATH = Path(__file__).resolve().with_name('domain_checker_chrome')
SCREENSHOT_HEADER = 'Screenshot'
VML = 'urn:schemas-microsoft-com:vml'
OFFICE = 'urn:schemas-microsoft-com:office:office'
EXCEL = 'urn:schemas-microsoft-com:office:excel'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
DOC_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def package_relationships(archive, part):
    folder, name = posixpath.split(part)
    relpath = f'{folder}/_rels/{name}.rels'
    if relpath not in archive.namelist():
        return {}
    return {
        node.get('Id'): posixpath.normpath(posixpath.join(folder, node.get('Target'))).lstrip('/')
        for node in ET.fromstring(archive.read(relpath))
        if node.get('TargetMode') != 'External'
    }


def note_drawings(archive):
    """Resolve worksheet and VML parts through actual package relationships."""
    relationships = package_relationships(archive, 'xl/workbook.xml')
    root = ET.fromstring(archive.read('xl/workbook.xml'))
    for sheet in root.findall(f'{{{MAIN}}}sheets/{{{MAIN}}}sheet'):
        part = relationships[sheet.get(f'{{{DOC_REL}}}id')]
        drawing = ET.fromstring(archive.read(part)).find(f'{{{MAIN}}}legacyDrawing')
        if drawing is not None:
            target = package_relationships(archive, part)[drawing.get(f'{{{DOC_REL}}}id')]
            yield sheet.get('name'), target


def load_screenshot_notes(path):
    """Retain embedded image notes that openpyxl otherwise discards on save."""
    images = {}
    with zipfile.ZipFile(path) as archive:
        for title, drawing in note_drawings(archive):
            relationships = package_relationships(archive, drawing)
            for shape in ET.fromstring(archive.read(drawing)).findall(f'{{{VML}}}shape'):
                fill = shape.find(f'{{{VML}}}fill')
                client = shape.find(f'{{{EXCEL}}}ClientData')
                if fill is None or client is None or client.get('ObjectType') != 'Note':
                    continue
                image_id = fill.get(f'{{{OFFICE}}}relid') or fill.get(f'{{{DOC_REL}}}id')
                if image_id in relationships:
                    row = int(client.findtext(f'{{{EXCEL}}}Row')) + 1
                    col = int(client.findtext(f'{{{EXCEL}}}Column')) + 1
                    images[title, row, col] = archive.read(relationships[image_id])
    return images


# Support this PC's standalone installation when running bbc.py directly.
LOCAL_DEPS = Path(__file__).resolve().parents[1] / '.domain_checker_deps'
if LOCAL_DEPS.is_dir() and any(importlib.util.find_spec(name) is None for name in ('playwright', 'prompt_toolkit')):
    sys.path.insert(0, str(LOCAL_DEPS))


def chrome_executable():
    candidates = [shutil.which('chrome'), shutil.which('google-chrome')]
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            candidates.append(str(Path(os.environ[variable]) / 'Google/Chrome/Application/chrome.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError('Google Chrome is required. Install Chrome, then run this script again.')


@contextmanager
def chrome_session(playwright, profile_path=PROFILE_PATH):
    """Attach Playwright to our own Chrome process; never use the personal profile."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    startup = None
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    process = subprocess.Popen([
        chrome_executable(), f'--user-data-dir={profile_path.resolve()}',
        f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
        '--no-first-run', '--no-default-browser-check', '--start-minimized',
        # Let a minimized page finish rendering and respond to search events.
        '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows', 'about:blank',
    ], startupinfo=startup, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        from playwright.sync_api import Error
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('The checker Chrome profile is already in use or Chrome could not start. Close the checker window and try again.')
            try:
                browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=2000)
                break
            except Error:
                time.sleep(0.5)
        if browser is None:
            raise RuntimeError('Could not connect to the separate Chrome session.')
        yield browser.contexts[0]
    finally:
        if browser is not None:
            try:
                browser.new_browser_cdp_session().send('Browser.close')
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


class SiteUnavailable(RuntimeError):
    """The search could not run; this is not a domain availability result."""


class SiteBlocked(SiteUnavailable):
    """GoDaddy denied access or requires an interactive verification."""


def save_pending(path, domains):
    """Keep unprocessed input on disk until its Excel row is saved."""
    if not domains:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(suffix='.json', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'domains': domains}, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_pending(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('domains'), list):
        raise ValueError(f'Invalid pending batch in {path}; file left intact.')
    if any(not isinstance(domain, str) for domain in state['domains']):
        raise ValueError(f'Invalid pending domains in {path}; file left intact.')
    return [normalize_domain(domain) for domain in state['domains']]


def assert_site_access(page, response=None):
    """Detect access failures separately from an unrecognized domain result."""
    from playwright.sync_api import Error
    if response is not None:
        if response.status in (401, 403, 429):
            raise SiteBlocked(f'GoDaddy returned HTTP {response.status}.')
        if response.status >= 400:
            raise SiteUnavailable(f'GoDaddy returned HTTP {response.status}.')
    if page.is_closed():
        raise SiteUnavailable('The browser page was closed before the search finished.')
    try:
        title = page.title().strip()
        body = page.locator('body').inner_text(timeout=1500).strip()
    except Error:
        # A document can briefly disappear while a search navigates.
        return
    blocked_title = re.search(r'\b(access denied|forbidden|too many requests|security check|just a moment)\b', title, re.I)
    blocked_body = re.search(
        r'\A(?:access denied|forbidden|too many requests)\b|'
        r"you (?:do not|don't) have permission to access|"
        r'verify (?:that )?you are (?:a )?human|'
        r'please (?:complete|solve) (?:the |this )?(?:security check|captcha)|'
        r'unusual traffic from your|your (?:access|request) has been blocked',
        body, re.I,
    )
    if blocked_title or blocked_body:
        raise SiteBlocked('GoDaddy denied access or requested a browser verification.')
    if re.search(r"\A(?:this site can.t be reached|service unavailable|internal server error)\b", body, re.I):
        raise SiteUnavailable('GoDaddy did not load a usable search page.')


def documents_path():
    if os.name == 'nt':
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / 'Documents'


def normalize_domain(value):
    value = value.strip().lower()
    if '://' in value:
        value = urlsplit(value).hostname or ''
    value = value.rstrip('./').encode('idna').decode('ascii')
    labels = value.split('.')
    if len(value) > 253 or len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in labels):
        raise ValueError('Expected a full domain such as example.com')
    return value


def save_atomic(workbook, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(suffix='.xlsx', dir=path.parent)
    os.close(handle)
    try:
        # openpyxl closes image streams during save. Refresh them for each row.
        for sheet in workbook:
            for image in sheet._images:
                if not hasattr(image, '_saved_png'):
                    image._saved_png = image._data()
                image.ref = BytesIO(image._saved_png)
        workbook.save(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def open_results(path):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill
    if path.exists():
        screenshots = load_screenshot_notes(path)
        workbook = load_workbook(path)
        sheet = workbook['Domains'] if 'Domains' in workbook.sheetnames else workbook.active
        if tuple(sheet.cell(1, c).value for c in range(1, 4)) != HEADERS:
            raise ValueError(f'Unexpected columns in {path}; existing file left intact.')
    else:
        workbook = Workbook()
        screenshots = {}
        sheet = workbook.active
        sheet.title = 'Domains'
        sheet.append(HEADERS)
        sheet.freeze_panes = 'A2'
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='16324F')
        for col, width in [('A', 48), ('B', 28), ('C', 26)]:
            sheet.column_dimensions[col].width = width
        sheet.auto_filter.ref = 'A1:C1'
    if sheet.cell(1, 4).value not in (None, SCREENSHOT_HEADER):
        raise ValueError(f'Column D is already used in {path}; existing file left intact.')
    sheet.cell(1, 4, SCREENSHOT_HEADER)
    sheet.cell(1, 4).font = Font(bold=True, color='FFFFFF')
    sheet.cell(1, 4).fill = PatternFill('solid', fgColor='16324F')
    sheet.column_dimensions['D'].width = 20
    sheet.auto_filter.ref = f'A1:D{sheet.max_row}'
    for (title, row, col), png in screenshots.items():
        if title == sheet.title and col == 4 and sheet.cell(row, col).value == 'Hover to view':
            add_screenshot_sheet(workbook, sheet, row, png)
    # Resize earlier screenshot sheets as well as new captures.
    for row in range(2, sheet.max_row + 1):
        link = sheet.cell(row, 4).hyperlink
        if link and link.location:
            target = link.location.rsplit('!', 1)[0].strip("'").replace("''", "'")
            if target.startswith('Screenshot ') and target in workbook.sheetnames:
                fit_screenshot_sheet(workbook[target])
    if not path.exists():
        save_atomic(workbook, path)
    return workbook, sheet


def screenshot_display_bounds():
    """Use the Excel window when available, reserving room for its UI and heading."""
    width, height = 1280, 720
    if os.name == 'nt':
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rectangle = wintypes.RECT()
        if user32.SystemParametersInfoW(48, 0, ctypes.byref(rectangle), 0):
            width, height = rectangle.right - rectangle.left, rectangle.bottom - rectangle.top
        user32.FindWindowW.restype = wintypes.HWND
        window = user32.FindWindowW('XLMAIN', None)
        if window and not user32.IsIconic(window) and user32.GetWindowRect(window, ctypes.byref(rectangle)):
            width = min(width, rectangle.right - rectangle.left)
            height = min(height, rectangle.bottom - rectangle.top)
        # These coordinates already use this process's logical pixel units.
    return max(100, width - 120), max(100, height - 260)


def fit_screenshot_sheet(sheet):
    from PIL import Image as PillowImage
    max_width, max_height = screenshot_display_bounds()
    for picture in sheet._images:
        if not hasattr(picture, '_saved_png'):
            picture._saved_png = picture._data()
        with PillowImage.open(BytesIO(picture._saved_png)) as original:
            width, height = original.size
        scale = min(1, max_width / width, max_height / height)
        picture.width, picture.height = width * scale, height * scale
        # Replace a loaded drawing anchor so its old dimensions cannot override sizing.
        picture.anchor = 'A4'
    sheet.sheet_view.zoomScale = 100


def add_screenshot_sheet(workbook, results, row, png):
    from openpyxl.drawing.image import Image
    from openpyxl.worksheet.hyperlink import Hyperlink
    from openpyxl.styles import Font
    from openpyxl.utils.cell import quote_sheetname
    image_sheet = workbook.create_sheet(f'Screenshot {row - 1}')
    image_sheet.sheet_view.showGridLines = False
    image_sheet.sheet_view.zoomScale = 100
    image_sheet.column_dimensions['A'].width = 30
    image_sheet['A1'] = 'Back to results'
    image_sheet['A1'].hyperlink = Hyperlink(
        ref='A1', location=f'{quote_sheetname(results.title)}!D{row}',
    )
    image_sheet['A1'].style = 'Hyperlink'
    image_sheet['A2'] = str(results.cell(row, 1).value)
    image_sheet['A2'].font = Font(bold=True, size=14)
    image_sheet.row_dimensions[2].height = 24
    image_sheet.freeze_panes = 'A4'
    picture = Image(BytesIO(png))
    picture._saved_png = png
    image_sheet.add_image(picture, 'A4')
    fit_screenshot_sheet(image_sheet)
    cell = results.cell(row, 4)
    cell.value = 'View screenshot'
    cell.comment = None
    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f'{quote_sheetname(image_sheet.title)}!A1')
    cell.style = 'Hyperlink'


def append_result(workbook, sheet, path, domain, status, price, screenshot=None):
    sheet.append((domain, status, price, 'Not captured'))
    if screenshot:
        add_screenshot_sheet(workbook, sheet, sheet.max_row, screenshot)
    sheet.auto_filter.ref = f'A1:D{sheet.max_row}'
    while True:
        try:
            save_atomic(workbook, path)
            return
        except PermissionError:
            input(f'Close {path.name} in Excel, then press Enter to save this result: ')


def find_result_card(page, domain):
    """Match the exact heading inside the individual card, never suggestions/bundles."""
    # Taken cards take precedence; an "available" banner may advertise another TLD.
    layouts = [
        ('[data-cy="dbsCard"], [data-cy="flattened-taken-exact"]', 'Taken'),
        ('[data-cy="ExactMatchPremium"]', 'Listed'),
        ('[data-cy="availableCard"], [data-cy="availcard"]', 'Unregistered/Available'),
    ]
    for selector, status in layouts:
        for card in page.locator(selector).all():
            if not card.is_visible():
                continue
            matched = False
            for heading in card.locator('[data-cy="two-tone-domain"], .domain-name').all():
                if not heading.is_visible():
                    continue
                try:
                    name = heading.inner_text().strip()
                    if status == 'Taken':
                        name = re.sub(r'\s+is\s+taken\s*$', '', name, flags=re.I)
                    matched = normalize_domain(re.sub(r'\s+', '', name)) == domain
                except (ValueError, UnicodeError):
                    matched = False
                if matched:
                    break
            if not matched:
                continue
            if status == 'Taken':
                return status, None, card
            for price in card.locator('[data-cy="pricing-main-price"]').all():
                if price.is_visible() and price.inner_text().strip():
                    return status, price.inner_text().strip(), card
    return None


def extract_result(page, domain):
    result = find_result_card(page, domain)
    return result[:2] if result else None


def capture_result_screenshot(page, domain):
    from playwright.sync_api import Error
    try:
        result = find_result_card(page, domain)
        target = result[2] if result else page
        return target.screenshot(type='png', animations='disabled', timeout=15000)
    except Error as exc:
        print(f'  Screenshot could not be captured: {str(exc).splitlines()[0]}', flush=True)
        return None


def wait_result(page, domain, seconds):
    from playwright.sync_api import Error
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            assert_site_access(page)
            result = extract_result(page, domain)
            if result:
                return result
        except Error:
            if page.is_closed():
                raise
        page.wait_for_timeout(500)
    return None


def check_domain(context, domain, timeout, *, page=None, previous_domain=None):
    from playwright.sync_api import Error
    owns_page = page is None
    if owns_page:
        page = context.new_page()
    page.set_default_timeout(20000)
    try:
        if previous_domain is None:
            response = page.goto('https://www.godaddy.com/en-ph', wait_until='domcontentloaded', timeout=45000)
            assert_site_access(page, response)
        # Repeating the same name requires a new document; a stale matching card
        # otherwise looks like a completed search immediately after Enter.
        if previous_domain == domain:
            response = page.reload(wait_until='domcontentloaded', timeout=45000)
            assert_site_access(page, response)
        else:
            search = page.locator(
                'input[data-testid="domain-search-box-input"], input#domain-search-box, '
                'input[type="search"][placeholder="Type the domain you want"]'
            ).first
            search.wait_for(state='visible')
            if previous_domain is None:
                page.wait_for_timeout(5000)
            search.fill(domain)
            page.wait_for_timeout(1500)
            search.press('Enter')
        result = wait_result(page, domain, timeout)
        if result:
            return result
        assert_site_access(page)
        if not page.locator('input#domain-search-box, input[data-testid="domain-search-box-input"]').first.is_visible():
            raise SiteUnavailable('GoDaddy did not load a usable search page.')
        print('  Search page loaded, but no matching supported result was found.')
        return 'No Data', None
    except Error as exc:
        assert_site_access(page)
        raise SiteUnavailable(str(exc).splitlines()[0]) from exc
    finally:
        if owns_page:
            page.close()


def read_domains():
    from prompt_toolkit import prompt
    print('Paste one domain or your entire list (commas, spaces or pasted lines). Press Enter to start.', flush=True)
    try:
        # Bracketed paste keeps a multiline list together. The physical Enter
        # key submits the complete buffer, without asking for an empty line.
        text = prompt('Domains > ', multiline=False)
    except EOFError:
        return []
    domains = []
    for item in re.split(r'[\s,;]+', text.strip()):
        if not item:
            continue
        try:
            domains.append(normalize_domain(item))
        except (ValueError, UnicodeError) as exc:
            print(f'Skipped {item!r}: {exc}')
    return domains


def read_or_resume_domains(pending_path):
    domains = load_pending(pending_path)
    if not domains:
        return read_domains()
    print(f'Found {len(domains)} unfinished domains from the previous run.')
    try:
        answer = input('Press Enter to resume, or type n to exit and keep them: ').strip().lower()
    except EOFError:
        answer = 'n'
    if answer in ('n', 'no'):
        print(f'Pending domains are retained in {pending_path}.')
        return []
    return domains


def run_batch(context, domains, workbook, sheet, path, pending_path, timeout, delay=10):
    """Stop on a site failure without turning unperformed checks into Excel rows."""
    save_pending(pending_path, domains)
    page = context.new_page()
    previous_domain = None
    try:
        for index, domain in enumerate(domains, 1):
            print(f'[{index}/{len(domains)}] {domain}', flush=True)
            try:
                status, price = check_domain(context, domain, max(1, timeout), page=page, previous_domain=previous_domain)
            except SiteUnavailable as exc:
                print(f'Paused: {exc}', flush=True)
                print(f'{len(domains) - index + 1} domains remain saved in {pending_path}.')
                print('No result was written for this failed search. Run the script again to resume.')
                return False
            screenshot = capture_result_screenshot(page, domain)
            append_result(workbook, sheet, path, domain, status, price, screenshot)
            save_pending(pending_path, domains[index:])
            previous_domain = domain
            print(f'  Saved: {status} | {price or "-"}', flush=True)
            if index < len(domains):
                time.sleep(max(5, delay))
        return True
    finally:
        page.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=35)
    parser.add_argument('--delay', type=float, default=10, help='Seconds between searches (minimum 5)')
    args = parser.parse_args()
    path = documents_path() / 'domains.xlsx'
    workbook, sheet = open_results(path)
    print(f'Results: {path}')
    try:
        if args.init_only:
            save_atomic(workbook, path)
            return
        domains = read_or_resume_domains(PENDING_PATH)
        if not domains:
            print('No domains entered.')
            return
        print(f'Starting {len(domains)} domain(s). Loading browser driver...', flush=True)
        # Persist the input before browser startup as startup itself can fail.
        save_pending(PENDING_PATH, domains)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            print('Starting separate Chrome session in the background...', flush=True)
            with chrome_session(playwright) as context:
                print('Chrome connected. Opening search tab...', flush=True)
                if not run_batch(context, domains, workbook, sheet, path, PENDING_PATH, args.timeout, args.delay):
                    return 2
    finally:
        workbook.close()
    print(f'Finished. Results saved to {path}')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nStopped. Previously saved rows and pending domains are retained.')
    except ImportError as exc:
        print(f'Missing dependency: {exc}. Run: python -m pip install playwright openpyxl prompt_toolkit Pillow')
        raise SystemExit(1)
    except Exception as exc:
        print(f'Error: {exc}')
        raise SystemExit(1)
