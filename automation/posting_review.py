"""Review requested posting values and report why any repair did not stick."""
from dataclasses import dataclass
from decimal import Decimal

from selenium.webdriver.support.ui import WebDriverWait

from automation.posting_description import normalize_description, description_detail
from automation.posting_numbers import parse_numeric_value
from automation.task_control import cancellable_sleep, check_cancelled


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    errors: tuple = ()

    @property
    def message(self):
        return '; '.join(self.errors)


def normalize_field_value(key, value):
    if key == 'description':
        return normalize_description(value)
    text = ' '.join(str(value or '').split()).casefold()
    if key == 'vehicle_type' and text.startswith('car'):
        return 'car'
    if key in {'mileage', 'price'}:
        amount = parse_numeric_value(value)
        # Invalid text must never match merely because both sides are invalid.
        return amount if amount is not None else Decimal('NaN')
    return text


def review_posting_fields(driver, supplied, refill, *, stage, labels,
                          verified_fields=None, visible_only=False,
                          defer_description=False, log=None, event=None,
                          account=None, timeout=90, settle_for=0.25,
                          snapshot_reader=None):
    # Facebook selects Location automatically; never review or refill it.
    supplied = {key: value for key, value in supplied.items() if key != 'location'}
    if snapshot_reader is None:
        from automation.reposting import read_edit_form_snapshot
        snapshot_reader = read_edit_form_snapshot
    verified = verified_fields if verified_fields is not None else set()

    def supplied_value(key):
        value = supplied.get(key)
        return str(value if value is not None else '').strip()

    def record(key, status, detail):
        field = labels.get(key, key)
        if log:
            log(field, f'review: {stage}', status, detail=detail)
        else:
            print(f'[AutoListing][FIELD REVIEW] {field} | {stage} | {status} | {detail}', flush=True)

    def detail(key, expected, actual, state):
        if key == 'description':
            return description_detail(actual, expected, state)
        return f'expected={str(expected)!r}; actual={str(actual)!r}; field state={state}'

    def issues_in(snapshot, report=False):
        issues = []
        for key, expected in supplied.items():
            state = snapshot.get(key, {}).get('state', 'unavailable')
            actual = snapshot.get(key, {}).get('value')
            actual = '' if actual is None else actual
            if key in {'exterior_color', 'interior_color'} or not supplied_value(key):
                if report:
                    record(key, 'SKIPPED', 'optional color review' if key.endswith('_color') else 'no value supplied by the app')
                continue
            if state == 'unavailable':
                reason = None
                if key in {'clean_title', 'vin'}:
                    reason = 'this optional control is not present on the current page'
                elif visible_only and key in verified:
                    reason = 'verified before Next; its editor is not present on this step'
                elif defer_description and key == 'description':
                    reason = 'Description is not present yet; it must be verified before Publish'
                if reason:
                    if report:
                        record(key, 'SKIPPED', reason)
                    continue
            if state == 'unavailable' or not str(actual).strip() or normalize_field_value(key, actual) != normalize_field_value(key, expected):
                issues.append((key, expected, actual, state))
                if report:
                    record(key, 'FAILED', detail(key, expected, actual, state))
            elif report:
                record(key, 'SUCCESS', detail(key, expected, actual, state))
        return issues

    def passed(snapshot):
        verified.update(key for key, field in snapshot.items()
                        if field.get('state') != 'unavailable' and supplied_value(key)
                        and normalize_field_value(key, field.get('value')) == normalize_field_value(key, supplied[key]))
        print(f'[AutoListing][FIELD REVIEW PASSED] {stage}: requested fields retained their values.', flush=True)
        if event:
            event('POST_REVIEW', 'COMPLETED', account=account, stage=stage)
        return ReviewResult(True)

    for attempt in range(1, 3):
        check_cancelled()
        snapshot = snapshot_reader(driver)
        issues = issues_in(snapshot, report=True)
        if not issues:
            cancellable_sleep(settle_for)
            snapshot = snapshot_reader(driver)
            issues = issues_in(snapshot)
            if not issues:
                return passed(snapshot)
            for key, expected, actual, state in issues:
                record(key, 'FAILED', 'value changed during the render check; ' + detail(key, expected, actual, state))
        # A parent dropdown repair can clear a child that matched on the first
        # snapshot. Walk all supplied keys in order and use a fresh snapshot
        # after each write, so those newly cleared children are fixed now.
        for key in supplied:
            pending = {item[0]: item for item in issues_in(snapshot)}
            if key not in pending:
                continue
            _, expected, actual, state = pending[key]
            check_cancelled()
            record(key, 'START', f'refill attempt {attempt}/2; ' + detail(key, expected, actual, state))
            try:
                if refill(key, labels.get(key, key), expected) is False:
                    raise RuntimeError('The field filler reported that the requested value was not retained.')
                snapshot = snapshot_reader(driver)
                current = snapshot.get(key, {})
                if current.get('state', 'unavailable') == 'unavailable':
                    # Keep the full rendering timeout for a replaced/missing
                    # editor. A present but wrong value needs another edit,
                    # rather than wasting 35 seconds waiting for it to change.
                    WebDriverWait(driver, timeout, poll_frequency=0.2).until(lambda browser:
                        snapshot_reader(browser).get(key, {}).get('state', 'unavailable') != 'unavailable')
                    snapshot = snapshot_reader(driver)
                    current = snapshot.get(key, {})
                if normalize_field_value(key, current.get('value')) != normalize_field_value(key, expected):
                    raise ValueError('The live field is still blank or mismatched after replacement; retrying without an extra timeout.')
                cancellable_sleep(settle_for)
                snapshot = snapshot_reader(driver)
                current = snapshot.get(key, {})
                if (current.get('state', 'unavailable') == 'unavailable' or
                        normalize_field_value(key, current.get('value')) != normalize_field_value(key, expected)):
                    raise ValueError('The field changed again after replacement; it must be corrected before proceeding.')
                record(key, 'SUCCESS', 'refill matched the requested value on the live form')
            except Exception as exc:
                check_cancelled()
                snapshot = snapshot_reader(driver)
                current = snapshot.get(key, {})
                record(key, 'FAILED', f'refill attempt {attempt}/2: {type(exc).__name__}: {exc!r}; ' +
                       detail(key, expected, current.get('value') or '', current.get('state', 'unavailable')))
        cancellable_sleep(settle_for)

    snapshot = snapshot_reader(driver)
    remaining = issues_in(snapshot, report=True)
    if not remaining:
        # Verify a fresh read after settling even on the final repair pass.
        cancellable_sleep(settle_for)
        snapshot = snapshot_reader(driver)
        remaining = issues_in(snapshot)
        if not remaining:
            return passed(snapshot)
    errors = tuple(f'{labels.get(key, key)}: ' + detail(key, expected, actual, state)
                   for key, expected, actual, state in remaining)
    message = f'{stage} form review failed after refill: ' + '; '.join(errors)
    print(f'[AutoListing][FIELD REVIEW FAILED] {message}', flush=True)
    if event:
        event('POST_REVIEW', 'FAILED', account=account, stage=stage, error=message,
              fields=[key for key, _, _, _ in remaining])
    return ReviewResult(False, (message,))
