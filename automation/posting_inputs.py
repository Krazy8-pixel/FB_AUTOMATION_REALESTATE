"""Replace controlled posting inputs without appending to restored values."""
import time

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation.posting_numbers import numeric_input_value, parse_numeric_value
from automation.posting_review import normalize_field_value
from automation.task_control import cancellable_sleep, check_cancelled


def fill_posting_input(driver, key, label, value, locator, *, log=None,
                       timeout=90, stable_for=0.3):
    """Write a requested amount/text, then verify fresh nodes after blur.

    Native clearing is never followed by typing unless the live control is
    actually empty. An atomic native-prototype setter is available when React
    restores the prior value or a formatting mask rejects an intermediate blank.
    """
    numeric = key in {'price', 'mileage'}

    def record(method, status, **details):
        if log:
            log(label, method, status, **details)
        else:
            print(f'[AutoListing][FIELD METHOD] {label} | method={method} | status={status} | {details}', flush=True)

    try:
        requested = numeric_input_value(value) if numeric else str(value if value is not None else '')
        expected_number = parse_numeric_value(value) if numeric else None
    except ValueError as exc:
        record('normalize requested input', 'FAILED', error=exc, detail=f'requested={value!r}')
        return False
    record('normalize requested input', 'SUCCESS', value=requested,
           detail=f'given by app={value!r}; writing numeric amount only' if numeric else 'writing the supplied text')

    def candidates(current, writable=True):
        check_cancelled()
        found = []
        for control in current.find_elements(*locator):
            try:
                if control.tag_name.lower() not in {'input', 'textarea'} or not control.is_displayed():
                    continue
                can_edit = (control.is_enabled() and not control.get_attribute('readonly')
                            and control.get_attribute('aria-readonly') != 'true'
                            and control.get_attribute('aria-disabled') != 'true')
                if writable and not can_edit:
                    continue
                found.append((not can_edit, control))
            except StaleElementReferenceException:
                continue
        found.sort(key=lambda item: item[0])
        return found[0][1] if found else False

    def ready(current):
        return candidates(current)

    def read_live():
        # The control can be replaced during an input/blur event. Read its live
        # .value, never the old node's attribute/defaultValue/textContent.
        def read(current):
            control = candidates(current, writable=False)
            if not control:
                return False
            try:
                result = current.execute_script('''
                    const el=arguments[0];
                    return el.isConnected ? {value: String(el.value ?? '')} : null;
                ''', control)
                return result or False
            except StaleElementReferenceException:
                return False
        result = read(driver)
        if result is False:
            result = WebDriverWait(driver, timeout, poll_frequency=0.1).until(read)
        return result['value']

    def matches(actual):
        if numeric:
            parsed = parse_numeric_value(actual)
            return expected_number is not None and parsed is not None and parsed == expected_number
        return normalize_field_value(key, actual) == normalize_field_value(key, requested)

    def actual_detail(actual):
        detail = f'requested={value!r}; writing={requested!r}; actual={actual!r}'
        if numeric:
            detail += f'; expected number={expected_number}; actual number={parse_numeric_value(actual)}'
        return detail

    def verify_retained():
        deadline = time.monotonic() + stable_for
        while True:
            check_cancelled()
            actual = read_live()
            if not matches(actual):
                return False, actual
            if time.monotonic() >= deadline:
                return True, actual
            cancellable_sleep(0.05)

    record('locate current editable input', 'START', detail=f'timeout={timeout}s')
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
    except Exception as exc:
        check_cancelled()
        record('locate current editable input', 'FAILED', error=exc)
        return False
    record('locate current editable input', 'SUCCESS')

    methods = ('keyboard select-all, clear, and replace',
               'atomic native value setter + input/change fallback',
               'keyboard selected-text overwrite fallback')
    for method in methods:
        check_cancelled()
        record(method, 'START', value=requested)
        try:
            control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
            if method == 'atomic native value setter + input/change fallback':
                driver.execute_script('''
                    const el=arguments[0], value=arguments[1]; el.focus();
                    const proto=el.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);
                    el.dispatchEvent(new InputEvent('input', {bubbles:true,composed:true,inputType:'insertReplacementText',data:value}));
                    el.dispatchEvent(new Event('change', {bubbles:true})); el.blur();
                ''', control, requested)
            else:
                control.click()
                control.send_keys(Keys.CONTROL, 'a')
                if method == 'keyboard select-all, clear, and replace':
                    control.send_keys(Keys.BACKSPACE)
                    actual = read_live()
                    blank = not actual.strip() if not numeric else not any(character.isdecimal() for character in actual)
                    if not blank:
                        raise RuntimeError('The field restored its previous value after clearing; no text was appended. ' + actual_detail(actual))
                    record('verify field is empty before typing', 'SUCCESS', detail=actual_detail(actual))
                    control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
                else:
                    selected = driver.execute_script('''
                        const el=arguments[0];
                        return el.selectionStart===0 && el.selectionEnd===el.value.length;
                    ''', control)
                    if not selected:
                        raise RuntimeError('Could not verify selection of the entire old value; no text was appended.')
                control.send_keys(requested)
                # React may have replaced the node during typing; blur the
                # current node so its committed value is the one we review.
                control = WebDriverWait(driver, timeout, poll_frequency=0.2).until(ready)
                control.send_keys(Keys.TAB)
            retained, actual = verify_retained()
            if retained:
                record(method, 'SUCCESS', detail='value retained after blur and fresh render checks; ' + actual_detail(actual))
                return True
            record(method, 'FAILED', detail='live value did not match; trying the next method immediately; ' + actual_detail(actual))
        except Exception as exc:
            check_cancelled()
            record(method, 'FAILED', error=exc)

    try:
        actual = read_live()
    except Exception as exc:
        record('all replacement methods', 'FAILED', error=exc)
        return False
    record('all replacement methods', 'FAILED', detail=actual_detail(actual))
    return False
