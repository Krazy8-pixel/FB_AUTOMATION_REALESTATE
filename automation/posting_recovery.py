"""Bounded recovery of cleared fields after a rendered action stays disabled."""
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


def action_button(driver, label):
    xpath = f"//*[(@role='button' or self::button) and (@aria-label='{label}' or normalize-space()='{label}' or .//span[normalize-space()='{label}'])]"
    return next((el for el in driver.find_elements(By.XPATH, xpath) if el.is_displayed()), None)


def enabled(button):
    return (button is not None and button.is_enabled()
            and str(button.get_attribute('aria-disabled') or '').lower() != 'true'
            and button.get_attribute('disabled') is None)


def wait_for_action(driver, label, repair, alternative=None):
    """Give normal rendering its full wait before considering any repairs."""
    def ready(current):
        button = action_button(current, label)
        if enabled(button):
            return (button,)
        if button is None and alternative and action_button(current, alternative) is not None:
            return (None,)
        return False

    try:
        return WebDriverWait(driver, 90).until(ready)[0]
    except TimeoutException:
        def rendered(current):
            button = action_button(current, label)
            if button is None:
                return False
            idle = current.execute_script('''
                const visible = el => !!el.getClientRects().length;
                return document.readyState === 'complete' &&
                  ![...document.querySelectorAll('[aria-busy="true"],[role="progressbar"],[aria-label="Loading"]')].some(visible);
            ''')
            return button if idle else False
        button = WebDriverWait(driver, 90).until(rendered)
        if enabled(button):
            return button
        if not repair():
            raise TimeoutException(f'{label} remains disabled; no cleared supplied fields were found.')
        return WebDriverWait(driver, 90).until(ready)[0]


def click_action(driver, label, repair, *, verify=None, alternative=None, log=None):
    """Recheck the form after readiness waits; log native/JS click outcomes."""
    from automation.task_control import check_cancelled

    def record(method, status, **details):
        if log:
            log(label, method, status, **details)
        else:
            print(f'[AutoListing][ACTION METHOD] {label} | {method} | {status} | {details}', flush=True)

    record('wait for enabled button', 'START', detail='up to three 90-second readiness/recovery waits')
    try:
        button = wait_for_action(driver, label, repair, alternative=alternative)
    except Exception as exc:
        check_cancelled()
        record('wait for enabled button', 'FAILED', error=exc)
        raise
    if button is None:
        record('wait for enabled button', 'SKIPPED', detail=f'{alternative} is present; no {label} step')
        return False
    record('wait for enabled button', 'SUCCESS')

    for method in ('native click', 'javascript click fallback'):
        check_cancelled()
        if verify is not None:
            record('verify form before click', 'START')
            try:
                if verify() is False:
                    raise RuntimeError(f'Form review failed; {label} was not clicked.')
            except Exception as exc:
                record('verify form before click', 'FAILED', error=exc)
                raise
            record('verify form before click', 'SUCCESS')
        # A repair/review can replace the action node. Reacquire it, then do
        # another review if waiting was necessary, before sending any click.
        button = action_button(driver, label)
        if not enabled(button):
            button = wait_for_action(driver, label, repair, alternative=alternative)
            if button is None:
                return False
            if verify is not None and verify() is False:
                raise RuntimeError(f'Form review failed; {label} was not clicked.')
            button = action_button(driver, label)
            if not enabled(button):
                raise TimeoutException(f'{label} changed or became disabled after form review; no click was sent.')
        record(method, 'START')
        try:
            if method == 'native click':
                button.click()
            else:
                driver.execute_script('arguments[0].click();', button)
        except Exception as exc:
            check_cancelled()
            record(method, 'FAILED', error=exc)
            if method == 'javascript click fallback':
                raise RuntimeError(f'{label} native and JavaScript click methods failed: {type(exc).__name__}: {exc!r}') from exc
            continue
        record(method, 'SUCCESS', detail='click returned without an exception')
        return True
    return False


def restore_empty_fields(driver, supplied, fill):
    from automation.reposting import EDIT_FIELDS, read_edit_form_snapshot
    restored = 0
    # Parent dropdowns can reset dependent controls. Recheck once after repair.
    for _ in range(2):
        changed = False
        for key, label in EDIT_FIELDS.items():
            if key == 'clean_title':
                continue
            if not str(supplied.get(key) or '').strip():
                continue
            field = read_edit_form_snapshot(driver).get(key, {})
            if field.get('state') == 'empty' and not str(field.get('value') or '').strip():
                fill(key, label, supplied[key])
                WebDriverWait(driver, 90).until(lambda current:
                    bool(str(read_edit_form_snapshot(current).get(key, {}).get('value') or '').strip()))
                print(f'[AutoListing] Restored cleared field: {label}', flush=True)
                restored += 1
                changed = True
        if not changed:
            break
    return restored
