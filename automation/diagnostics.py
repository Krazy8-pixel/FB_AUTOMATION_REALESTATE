"""Readable, timestamped workflow markers; diagnostics never change outcomes."""
from datetime import datetime, timezone
from functools import wraps
import json
import re
from uuid import uuid4


_CHECK_LINE = re.compile(r'^\[CHECK\]\s+(\S+)\s+(\S+)\s+(\S+)\s+(\{.*\})\s*$')


def _readable_summary(text):
    """Turn structured workflow markers into a short, plain-language report header."""
    events = []
    for line in text.splitlines():
        match = _CHECK_LINE.match(line)
        if not match:
            continue
        timestamp, action, status, raw = match.groups()
        if action.startswith('GROUP_IMPORT'):
            continue
        try:
            details = json.loads(raw)
        except (TypeError, ValueError):
            details = {}
        events.append((timestamp, action, status, details))

    if not events:
        return ('PLAIN-LANGUAGE SUMMARY\n'
                'No real-estate workflow activity markers were found in the retained report history. '
                'The detailed log may still contain useful output.\n\n')

    counts = {}
    passed_statuses = {'FINISHED', 'POSTED', 'DELETED', 'SAVED', 'COMPLETED', 'HEALTHY',
                       'CONFIRMED_BY_UI', 'PUBLISH_CONFIRMED'}
    issue_statuses = {'STOPPED', 'FAILED', 'RETURNED_FALSE', 'REVIEW', 'CLICK_FAILED', 'SKIPPED',
                      'EXPIRED', 'FAILED_TO_START', 'TIMED_OUT', 'CANCELLED', 'NOT_FOUND',
                      'BROWSER_CLOSE_UNVERIFIED'}
    passed_count = sum(1 for _, _, status, _ in events if status in passed_statuses)
    issue_count = sum(1 for _, _, status, _ in events if status in issue_statuses)
    field_counts = {'SUCCESS': 0, 'FAILED': 0, 'SKIPPED': 0}
    for line in text.splitlines():
        for status in field_counts:
            if '[AutoListing][FIELD ' + status + ']' in line:
                field_counts[status] += 1
    publish_clicked = sum(1 for _, action, status, _ in events
                          if action == 'PUBLISH' and status == 'CLICKED')
    publish_click_failed = sum(1 for _, action, status, _ in events
                               if action == 'PUBLISH' and status == 'CLICK_FAILED')
    publish_click_only = sum(1 for _, action, status, _ in events
                             if action == 'PUBLISH' and status == 'CLICK_ONLY_REVIEW_ON_FACEBOOK')
    for _, action, status, _ in events:
        key = (action, status)
        counts[key] = counts.get(key, 0) + 1

    lines = [
        'PLAIN-LANGUAGE SUMMARY',
        'This report contains the application’s recorded activity. “Completed” means the automation step reported success; for a Publish action, it means the Publish click returned without an error.',
        f'Recorded workflow events: {len(events)}.',
        f'Recorded completion events: {passed_count}; events needing attention or ending early: {issue_count}.',
    ]
    if publish_clicked or publish_click_failed:
        lines.append(f'Publish button: {publish_clicked} click(s) returned successfully; {publish_click_failed} click attempt(s) failed.')
    if publish_click_only:
        lines.append(f'Publish follow-up: {publish_click_only} post(s) used the click-success rule without checking Facebook’s follow-up screen. These are recorded as posted by the app.')
    if any(field_counts.values()):
        lines.append('Posting form fields: '
                     f"{field_counts['SUCCESS']} filled successfully, "
                     f"{field_counts['FAILED']} failed, {field_counts['SKIPPED']} skipped (some fields are optional).")
    important = []
    for (action, status), count in sorted(counts.items()):
        if status in {'STOPPED', 'FAILED', 'RETURNED_FALSE', 'REVIEW', 'CLICK_FAILED', 'SKIPPED', 'EXPIRED',
                      'FAILED_TO_START', 'TIMED_OUT', 'CANCELLED', 'BROWSER_CLOSE_UNVERIFIED'}:
            important.append((action, status, count))
    if important:
        lines.append('Items to review:')
        action_names = {
            'POST': 'Listing post', 'IMPORT': 'Listing import',
            'IMPORT_LISTING': 'Imported listing', 'CROSS_POST': 'Cross-post',
            'PROPERTY_POST': 'Property post', 'PROPERTY_LOCATION': 'Property location',
            'PROPERTY_MEDIA': 'Property photos', 'PROPERTY_BACKUP_EDIT': 'Property backup edit',
            'PROPERTY_BACKUP': 'Property backup',
            'REPOST': 'Repost', 'DELETE_LISTINGS': 'Facebook deletion',
            'DELETE_LISTING': 'Facebook listing deletion',
            'REFRESH_CLICKS': 'Click-count refresh', 'PUBLISH': 'Publish button',
            'GROUP_ADD_BY_LINK': 'Add group by link',
            'GROUP_POST': 'Post to groups', 'GROUP_POST_ITEM': 'Individual group post',
            'SESSION_CHECK': 'Account session check', 'SESSION_CHECK_ACCOUNT': 'Account session',
            'FACEBOOK_LOGIN': 'Facebook login', 'CDP_LOGIN': 'Facebook account login',
            'DELETE_BY_STATUS': 'Delete listings by status', 'SCHEDULED_POST': 'Scheduled Facebook post',
        }
        for action, status, count in important:
            label = {
                'STOPPED': 'stopped or was cancelled; see details below',
                'FAILED': 'failed',
                'RETURNED_FALSE': 'returned an unsuccessful result',
                'CLICK_FAILED': 'Publish click attempts failed',
                'REVIEW': 'finished with details that need checking',
                'EXPIRED': 'Facebook sessions need login again',
                'FAILED_TO_START': 'could not start',
                'TIMED_OUT': 'timed out',
                'CANCELLED': 'was cancelled',
                'SKIPPED': 'was skipped; see details below',
                'BROWSER_CLOSE_UNVERIFIED': 'browser closure could not be verified',
            }.get(status, status.lower())
            lines.append(f"  - {action_names.get(action, action.replace('_', ' ').capitalize())}: {count} {label}.")
    else:
        if field_counts['FAILED']:
            lines.append('No workflow failure markers were recorded; see the posting form field failures listed above.')
        else:
            lines.append('No structured failures or review flags were recorded.')

    problem_events = [event for event in events
                      if event[2] in {'STOPPED', 'FAILED', 'RETURNED_FALSE', 'REVIEW', 'CLICK_FAILED',
                                      'NOT_FOUND', 'EXPIRED', 'FAILED_TO_START', 'TIMED_OUT', 'CANCELLED',
                                      'SKIPPED', 'BROWSER_CLOSE_UNVERIFIED'}]
    if problem_events:
        lines.append('Details for those items:')
        names = {
            'POST': 'Facebook post', 'IMPORT': 'Listing import',
            'IMPORT_LISTING': 'Imported listing', 'CROSS_POST': 'Cross-post',
            'PROPERTY_POST': 'Property post', 'PROPERTY_LOCATION': 'Property location',
            'PROPERTY_MEDIA': 'Property photos', 'PROPERTY_BACKUP_EDIT': 'Property backup edit',
            'PROPERTY_BACKUP': 'Property backup',
            'REPOST': 'Repost', 'DELETE_LISTINGS': 'Facebook deletion',
            'DELETE_LISTING': 'Facebook listing deletion',
            'REFRESH_CLICKS': 'Click-count refresh',
            'REFRESH_CLICKS_LISTING': 'Listing click-count refresh',
            'PUBLISH': 'Publish button',
            'GROUP_ADD_BY_LINK': 'Add group by link', 'GROUP_POST': 'Post to groups',
            'GROUP_POST_ITEM': 'Group post', 'SESSION_CHECK': 'Account session check',
            'SESSION_CHECK_ACCOUNT': 'Account session', 'FACEBOOK_LOGIN': 'Facebook login',
            'DELETE_BY_STATUS': 'Delete listings by status',
            'CDP_LOGIN': 'Facebook account login',
            'SCHEDULED_POST': 'Scheduled Facebook post',
        }
        for _, action, status, details in problem_events[-20:]:
            description = names.get(action, action.replace('_', ' ').capitalize())
            account = details.get('account') or details.get('target_account')
            title = details.get('title')
            listing_id = details.get('listing_id')
            reason = details.get('error') or details.get('result') or details.get('reason')
            missing = details.get('missing_fields') or []
            if account:
                description += f' for {account}'
            if title:
                description += f' — {title}'
            elif details.get('group_link'):
                description += f" — {details.get('group_link')}"
            elif listing_id:
                description += f' — listing ID {listing_id}'
            if missing:
                description += f" — details to check: {', '.join(missing)}"
            if reason:
                description += f' — reason: {str(reason).replace(chr(10), " ")[:220]}'
            elif 'cancel' in str(details.get('error_type') or '').casefold():
                description += ' — the run was cancelled'
            elif status == 'REVIEW' and action == 'REFRESH_CLICKS':
                description += f" — refreshed {details.get('confirmed', 0)} of {details.get('total', 0)} listings"
            lines.append(f'  - {description}.')

    lines.append('Most recent activity:')
    for timestamp, action, status, details in events[-12:]:
        if action.startswith('GROUP_IMPORT'):
            continue
        account = details.get('account') or details.get('target_account') or ''
        title = details.get('title') or ''
        error = details.get('error') or ''
        description = f'{timestamp} — {action}: {status}'
        if account:
            description += f' — account {account}'
        if title:
            description += f' — {title}'
        if error:
            description += f' — reason: {str(error).replace(chr(10), " ")[:220]}'
        if action == 'REFRESH_CLICKS_SCAN':
            description += (f" — scan {details.get('scan', '?')}: "
                            f"{details.get('candidates', 0)} cards, "
                            f"{details.get('confirmed', 0)}/{details.get('total', 0)} confirmed, "
                            f"{details.get('missing_click_text', 0)} without click text, "
                            f"{details.get('unmatched', 0)} unmatched")
        lines.append('  - ' + description)
    lines.extend([
        'How to use this report: start with “Items to review” and “Most recent activity”, then search the detailed log below for the same account or listing to see the steps and error message.',
        'This report keeps a rolling history; very old activity can eventually roll off. The detailed log may contain account names, listing descriptions, and temporary media links. Share it only with people helping troubleshoot.',
        '',
    ])
    return '\n'.join(lines) + '\n'


def event(action, status, **details):
    try:
        print(f'[CHECK] {datetime.now(timezone.utc).isoformat(timespec="seconds")} '
              f'{action} {status} {json.dumps(details, ensure_ascii=False, default=str)}', flush=True)
    except Exception:
        pass


def traced(action):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            run_id = uuid4().hex[:12]
            account = kwargs.get('account') or (args[1] if action == 'POST' and len(args) > 1 else args[0] if args else '')
            event(action, 'START', run_id=run_id, account=account)
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                event(action, 'STOPPED', run_id=run_id, account=account,
                      error_type=type(exc).__name__, error=str(exc))
                raise
            failed_result = (result is False or
                             isinstance(result, dict) and result.get('ok') is False or
                             isinstance(result, tuple) and bool(result) and result[0] is False)
            outcome = result if isinstance(result, (str, int, float, bool)) else None
            if isinstance(result, tuple):
                outcome = result[2] if len(result) > 2 and isinstance(result[2], str) else None
            elif isinstance(result, dict):
                outcome = result.get('message') or result.get('result')
            event(action, 'RETURNED_FALSE' if failed_result else 'FINISHED',
                  run_id=run_id, account=account,
                  records=len(result) if isinstance(result, list) else None,
                  result=outcome)
            return result
        return run
    return decorate
