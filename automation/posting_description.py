"""Locate, fill, and read the same live Description editor across FB layouts."""
import time
import unicodedata

from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation.task_control import cancellable_sleep, check_cancelled


# Shared with the form snapshot reader. Never choose a hidden duplicate or a
# textarea belonging to another field merely because it is first/last in DOM.
DESCRIPTION_CONTROL_JS = r"""(root) => {
    const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const visible = el => !!el && el.isConnected && !!el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' && !el.closest('[aria-hidden="true"],[inert]');
    const isDescription = text => /^description(?:\s*[:*(]|\s+\(?optional\)?|$)/i.test(norm(text));
    const controls = [...root.querySelectorAll('textarea,input:not([type="hidden"]),[contenteditable="true"]')]
        .filter(visible).filter(el => !el.closest('[role="listbox"]'));
    const candidates = [];
    for (const el of controls) {
        const textArea = el.tagName === 'TEXTAREA';
        const editable = el.isContentEditable;
        let score = 0;
        if (isDescription(el.getAttribute('aria-label'))) score = 120;
        const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => document.getElementById(id)?.textContent || '').join(' ');
        if (isDescription(labelled)) score = Math.max(score, 115);
        if ([...(el.labels || [])].some(label => isDescription(label.textContent))) score = Math.max(score, 110);
        if (isDescription(el.getAttribute('placeholder'))) score = Math.max(score, 85);
        if (/description/i.test(el.getAttribute('name') || '')) score = Math.max(score, 90);
        // Facebook also uses a sibling <span>Description</span> in plain divs.
        for (let node = el.parentElement, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
            const editors = [...node.querySelectorAll('textarea,input:not([type="hidden"]),[contenteditable="true"]')].filter(visible);
            if (editors.length !== 1) break;
            const labels = [...node.querySelectorAll('span,label')].filter(visible);
            if (labels.some(label => isDescription(label.textContent))) {
                score = Math.max(score, 100 - depth);
                break;
            }
        }
        if (score) {
            if (el.closest('[role="dialog"]')) score += 3;
            if (textArea || editable) score += 2;
            const writable = !el.disabled && !el.readOnly && el.getAttribute('aria-disabled') !== 'true' &&
                el.getAttribute('aria-readonly') !== 'true';
            candidates.push({el, score, writable});
        }
    }
    candidates.sort((a, b) => Number(b.writable) - Number(a.writable) || b.score - a.score);
    if (candidates.length) return candidates[0].el;
    const textareas = controls.filter(el => el.tagName === 'TEXTAREA');
    if (textareas.length === 1) {
        const el = textareas[0];
        const named = norm(el.getAttribute('aria-label')) || norm(el.getAttribute('aria-labelledby')) ||
            norm(el.getAttribute('name')) || [...(el.labels || [])].some(label => norm(label.textContent));
        return named ? null : el;
    }
    return null;
}"""


def normalize_description(value):
    text = unicodedata.normalize('NFC', str(value or ''))
    return ' '.join(text.replace('\u200b', '').replace('\ufeff', '').split())


def find_description_control(driver):
    return driver.execute_script('return (' + DESCRIPTION_CONTROL_JS + ')(document);')


def read_description(driver):
    return driver.execute_script('''
        const el = (''' + DESCRIPTION_CONTROL_JS + ''')(document);
        if (!el) return {value: '', state: 'unavailable'};
        const value = el.matches('input,textarea') ? el.value : (el.innerText ?? el.textContent ?? '');
        return {value: String(value || ''), state: el.disabled || el.readOnly ? 'disabled' : value ? 'read' : 'empty'};
    ''') or {'value': '', 'state': 'unavailable'}


def description_detail(value, expected=None, state=None):
    text = str(value or '')
    preview = text[:100].replace('\r', '\\r').replace('\n', '\\n')
    detail = f'actual={len(text)} characters {preview!r}'
    if expected is not None:
        detail = f'expected={len(str(expected))} characters; ' + detail
    if state:
        detail += f'; field state={state}'
    return detail


def fill_description(driver, value, log=None, timeout=90, stable_for=0.6):
    """Try real editing fallbacks and verify fresh nodes after blur/rerenders."""
    expected = str(value or '')
    wanted = normalize_description(expected)
    if not wanted:
        return True

    def record(method, status, **details):
        if log:
            log('Description', method, status, **details)
        else:
            print(f'[AutoListing][FIELD METHOD] Description | method={method} | status={status} | {details}', flush=True)

    def ready(current):
        check_cancelled()
        control = find_description_control(current)
        if control is None:
            return False
        return control if (control.is_enabled() and not control.get_attribute('readonly')
                           and control.get_attribute('aria-readonly') != 'true'
                           and control.get_attribute('aria-disabled') != 'true') else False

    record('locate live visible editor', 'START', detail=f'timeout={timeout}s')
    try:
        if find_description_control(driver) is None:
            record('scroll to Description', 'START', detail='editor not yet present')
            driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
        record('locate live visible editor', 'SUCCESS', detail=f'tag={control.tag_name}; aria-label={control.get_attribute("aria-label")!r}')
    except Exception as exc:
        check_cancelled()
        record('locate live visible editor', 'FAILED', error=exc)
        return False

    contenteditable = control.tag_name.lower() not in {'input', 'textarea'}
    methods = (['keyboard replace', 'contenteditable insertText', 'native value setter + input/change']
               if contenteditable else ['native value setter + input/change', 'keyboard replace'])

    for method in methods:
        check_cancelled()
        record(method, 'START', detail=f'writing {len(expected)} characters')
        try:
            # Every fallback gets the current node; React can replace an editor
            # after clearing or during an input/blur handler.
            control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
            if method == 'keyboard replace':
                control.click()
                control.send_keys(Keys.CONTROL, 'a')
                control.send_keys(Keys.BACKSPACE)
                control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
                control.send_keys(expected)
                control.send_keys(Keys.TAB)
            elif method == 'contenteditable insertText':
                driver.execute_script('''
                    const el=arguments[0]; el.focus();
                    const selection=window.getSelection(), range=document.createRange();
                    range.selectNodeContents(el); selection.removeAllRanges(); selection.addRange(range);
                    if (!document.execCommand('insertText', false, arguments[1]))
                        throw new Error('Browser did not accept insertText');
                    el.dispatchEvent(new Event('change', {bubbles:true})); el.blur();
                ''', control, expected)
            else:
                driver.execute_script('''
                    const el=arguments[0], value=arguments[1]; el.focus();
                    if (el.matches('input,textarea')) {
                        const proto=el.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);
                    } else el.textContent=value;
                    el.dispatchEvent(new InputEvent('input', {bubbles:true,composed:true,inputType:'insertText',data:value}));
                    el.dispatchEvent(new Event('change', {bubbles:true})); el.blur();
                ''', control, expected)

            retained_until = time.monotonic() + stable_for
            actual = read_description(driver)
            while normalize_description(actual.get('value')) == wanted:
                check_cancelled()
                if time.monotonic() >= retained_until:
                    record(method, 'SUCCESS', detail='text matches after blur and remained stable on fresh reads')
                    return True
                cancellable_sleep(0.1)
                actual = read_description(driver)
            record(method, 'FAILED', detail='text was cleared or changed after editing; ' +
                   description_detail(actual.get('value'), expected, actual.get('state')))
        except Exception as exc:
            check_cancelled()
            record(method, 'FAILED', error=exc)

    actual = read_description(driver)
    record('all description methods', 'FAILED', detail=description_detail(
        actual.get('value'), expected, actual.get('state')))
    return False
