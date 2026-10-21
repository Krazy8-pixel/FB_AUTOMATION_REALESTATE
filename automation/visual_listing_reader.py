"""Experimental screenshot/OCR reader for source-only import testing.

Requires local Tesseract (TESSERACT_PATH or PATH). Values are suggestions for
review, not approved repost data. An optional vision callback can be added later.
"""
import csv
import io
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
import os
import sys

from automation.reposting import EDIT_FIELDS, ITEM_EDIT_FIELDS, EXTRA_VEHICLE_FIELDS, _safe_image_urls, wait_for_edit_form_ready, read_edit_form_snapshot

OPTIONAL_COLOR_FIELDS = {'exterior_color', 'interior_color'}


def recognition_proposals(path, ocr, vision, warnings):
    """Recognition failures must not discard usable form control values."""
    try:
        lines = ocr(path)
        proposals = parse_ocr_fields(lines)
    except Exception as exc:
        lines, proposals = [], {}
        warnings.append(f'OCR unavailable for {path.name}: {exc}; using form controls.')
    if vision:
        try:
            for key, value in vision(path).items():
                if key in dict(EDIT_FIELDS, **ITEM_EDIT_FIELDS, **EXTRA_VEHICLE_FIELDS) and isinstance(value, str) and value.strip():
                    proposals.setdefault(key, {'value': value.strip(), 'state': 'needs_review',
                                               'confidence': None, 'evidence': ['vision proposal']})
        except Exception as exc:
            warnings.append(f'Vision unavailable for {path.name}: {exc}; using other readers.')
    return lines, proposals


def tesseract_ocr(path):
    from automation.runtime_paths import resource_root
    local = resource_root() / ('ocr_runtime' if getattr(sys, 'frozen', False) else '.ocr_runtime') / 'tesseract.exe'
    executable = os.getenv('TESSERACT_PATH') or (str(local) if local.is_file() else shutil.which('tesseract'))
    if not executable:
        raise RuntimeError('Local OCR runtime is missing. Install Tesseract OCR or set TESSERACT_PATH.')
    result = subprocess.run([executable, str(path), 'stdout', '-l', 'eng', '--psm', '11', 'tsv'],
                            capture_output=True, text=True, encoding='utf-8', timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('OCR failed: ' + result.stderr.strip())
    lines = {}
    for word in csv.DictReader(io.StringIO(result.stdout), delimiter='\t'):
        text = word.get('text', '').strip()
        if not text:
            continue
        key = tuple(word[name] for name in ('page_num', 'block_num', 'par_num', 'line_num'))
        line = lines.setdefault(key, {'words': [], 'confidence': [], 'top': int(word['top']), 'left': int(word['left'])})
        line['words'].append(text)
        line['confidence'].append(float(word['conf']))
    return [{'text': ' '.join(line['words']), 'confidence': min(line['confidence']),
             'top': line['top'], 'left': line['left']} for line in sorted(lines.values(), key=lambda row: (row['top'], row['left']))]


def parse_ocr_fields(lines):
    """Conservative label/value pairing. Preserve confidence and raw evidence."""
    aliases = {field: [label] for field, label in dict(EDIT_FIELDS, **ITEM_EDIT_FIELDS, **EXTRA_VEHICLE_FIELDS).items()}
    aliases['exterior_color'].append('Exterior colour')
    aliases['interior_color'].append('Interior colour')
    aliases['mileage'].append('Odometer')
    aliases['model'].append('Mode!')
    aliases['vehicle_type'].append('ehicle type')
    found = {}
    labels = {name.casefold() for names in aliases.values() for name in names}
    for index, line in enumerate(lines):
        text = line['text'].strip()
        for field, names in aliases.items():
            # Checkbox state is not represented by its explanatory text.
            if field == 'clean_title':
                continue
            for label in names:
                match = re.match(r'^' + re.escape(label) + r'(?:\s*[:：]\s*|\s+|$)(.*)$', text, re.I)
                if not match:
                    continue
                value = match.group(1).strip()
                evidence = [text]
                confidence = float(line.get('confidence', 0))
                if not value and index + 1 < len(lines):
                    candidates = lines[index + 1:index + 4]
                    following = next((row for row in candidates if
                        'top' not in line or ('top' in row and
                        0 <= row['top'] - line['top'] <= 80 and
                        abs(row.get('left', 0) - line.get('left', 0)) <= 25)), None)
                    if following is None:
                        continue
                    candidate = following['text'].strip()
                    if 'top' in line and 'top' in following and (
                        abs(following.get('left', 0) - line.get('left', 0)) > 120
                        or not 0 <= following['top'] - line['top'] <= 80
                    ):
                        continue
                    # Never consume the next field label as the current value.
                    if not any(candidate.casefold() == name or candidate.casefold().startswith(name + ':') for name in labels):
                        value = candidate
                        evidence.append(candidate)
                        confidence = min(confidence, float(following.get('confidence', 0)))
                if not value or value.casefold().startswith(('select ', 'enter ', 'choose ')):
                    continue
                if any(marker in value.casefold() for marker in (
                    'will appear here', 'tell buyers anything', 'marketplace items are public',
                    'this vehicle has no significant damage')):
                    continue
                if field == 'location' and not re.search(r'[A-Za-z]', value):
                    continue
                if field in ('year', 'mileage', 'price') and not re.search(r'\d', value):
                    continue
                if field == 'model' and value.isdecimal() and len(value) > 8:
                    continue  # Facebook internal ID, not a model name.
                found[field] = {'value': value, 'state': 'needs_review', 'confidence': confidence, 'evidence': evidence}
    return found


def merge_control_fields(fields, snapshot, screenshot):
    """Actual control values take precedence over lossy screenshot text.

    Preserve OCR evidence and disagreements without substituting helper text
    for empty controls. Unavailable controls remain explicit.
    """
    for key, control in snapshot.items():
        if key not in dict(EDIT_FIELDS, **ITEM_EDIT_FIELDS, **EXTRA_VEHICLE_FIELDS) or control.get('state') == 'unavailable':
            continue
        value = str(control.get('value') or '').strip()
        if key == 'model' and value.isdecimal() and len(value) > 8:
            continue
        previous = fields.get(key)
        proposal = {'value': value, 'state': control.get('state', 'read'),
                    'confidence': None, 'source': 'form_control',
                    'evidence': ['Actual listing control value'], 'screenshot': str(screenshot)}
        if previous:
            evidence = previous if previous.get('source') != 'form_control' else previous.get('ocr_evidence')
            if evidence:
                proposal['ocr_evidence'] = evidence
            if evidence and evidence['value'] != value:
                proposal['ocr_disagreement'] = True
        fields[key] = proposal


def extract_video_sources(driver):
    """Read actual video/source URLs. Never mistake poster photos for videos."""
    sources = driver.execute_script("""
      return [...document.querySelectorAll('video')].flatMap(video =>
        [video.currentSrc, video.getAttribute('src'), ...[...video.querySelectorAll('source')].map(s=>s.src)]
      ).filter(Boolean);
    """) or []
    direct, unresolved = [], []
    for url in dict.fromkeys(sources):
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and (parsed.hostname or '').endswith('.fbcdn.net'):
            direct.append(url)
        else:
            unresolved.append({'url': url, 'reason': 'Blob, streaming, or unsupported source; no original downloadable video recovered.'})
    return direct, unresolved


def capture_description_scroll(driver, output_dir, max_captures=24):
    """Capture the textarea's own viewport without losing its full DOM value."""
    captures = []
    controls = driver.find_elements('xpath', "//textarea | //*[@contenteditable='true' and @role='textbox']")
    for control in controls:
        if not control.is_displayed():
            continue
        sizes = driver.execute_script('return [arguments[0].scrollTop,arguments[0].scrollHeight,arguments[0].clientHeight];', control)
        if not isinstance(sizes, list) or len(sizes) != 3 or sizes[1] <= sizes[2] + 2:
            continue
        try:
            driver.execute_script('arguments[0].scrollIntoView({block:"center"}); arguments[0].scrollTop=0;', control)
            for index in range(max_captures):
                path = Path(output_dir) / f'description-{index:02d}.png'
                if control.screenshot(str(path)):
                    captures.append({'path': str(path.resolve()), 'kind': 'description_scroll'})
                before, after = driver.execute_script('const el=arguments[0],before=el.scrollTop; el.scrollTop+=Math.max(30,el.clientHeight*.7); return [before,el.scrollTop];', control)
                if before == after:
                    break
                time.sleep(.2)
        finally:
            driver.execute_script('arguments[0].scrollTop=arguments[1];', control, sizes[0])
        break
    return captures


def read_listing_edit_visual(driver, output_dir, *, ocr=tesseract_ocr, vision=None, max_captures=12,
                             allow_unstructured=False, max_seconds=90, include_media=True):
    """Read the already-open edit page without changing or submitting fields.

    Returns the existing posts structure plus review evidence. `vision`, when
    supplied, takes a screenshot Path and returns {field: string}; its proposals
    remain unverified. Scroll position is restored even if recognition fails.
    Media extraction uses DOM URLs independently of screenshot recognition.
    """
    parsed = urlsplit(driver.current_url)
    if parsed.hostname not in ('facebook.com', 'www.facebook.com', 'm.facebook.com') or not parsed.path.startswith('/marketplace/edit/'):
        raise ValueError('Open the Facebook listing edit page before running this experimental reader.')
    if not 1 <= max_captures <= 30:
        raise ValueError('max_captures must be between 1 and 30')
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # An unrelated search input exists while Facebook is still loading.
    # Wait for populated, stable listing controls before choosing the panel.
    if not allow_unstructured:
        wait_for_edit_form_ready(driver, timeout=45)
    # Find the edit form's scroll container; Facebook may scroll a panel rather
    # than the document. This examines layout only, not field values.
    scroll = driver.execute_script(r"""
      const label = el => el.getAttribute('aria-label') ||
        [...(el.labels || [])].map(node=>node.textContent).join(' ') ||
        (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>document.getElementById(id)?.textContent || '').join(' ');
      let el=[...document.querySelectorAll('input')].find(node=>/\bprice\b/i.test(label(node)));
      if (!el) {
        const roots=[...document.querySelectorAll('[role="dialog"],[role="main"]')];
        const root=roots.reverse().find(node => /edit listing/i.test(node.innerText || ''));
        if (!root) throw new Error('A listing edit panel was not found');
        return root.querySelector('[role="form"]') || root;
      }
      for(let node=el;node;node=node.parentElement)
        if(node.clientHeight>100 && node.scrollHeight>node.clientHeight+2 && /auto|scroll/.test(getComputedStyle(node).overflowY)) return node;
      return el.closest('form') || el.closest('[role="dialog"]') || el.closest('[role="main"]') || document.scrollingElement;
    """)
    original = driver.execute_script('return arguments[0].scrollTop;', scroll)
    fields, images, videos, unresolved, captures, warnings = {}, [], [], [], [], []
    complete = False
    deadline = time.monotonic() + max_seconds
    print(f'[ListingImport][OCR] Starting edit-page read: {driver.current_url}', flush=True)
    try:
        driver.execute_script('arguments[0].scrollTop=0;', scroll)
        for index in range(max_captures):
            if time.monotonic() >= deadline:
                warnings.append('Visual read time limit reached; continuing with recovered fields.')
                break
            print(f'[ListingImport][OCR] Capture {index + 1}/{max_captures}: waiting for page, then taking screenshot', flush=True)
            time.sleep(.5)
            path = output / f'edit-{index:02d}.png'
            if not driver.save_screenshot(str(path)):
                raise RuntimeError('Screenshot capture failed')
            # Retain the full screenshot for diagnostics; recognize only the
            # form panel so preview placeholders cannot become field values.
            bounds = driver.execute_script('''
              const r=arguments[0].getBoundingClientRect();
              return {left:Math.max(0,r.left), top:Math.max(0,r.top),
                right:Math.min(innerWidth,r.right), bottom:Math.min(innerHeight,r.bottom),
                width:innerWidth,height:innerHeight};
            ''', scroll)
            if isinstance(bounds, dict):
                from PIL import Image
                with Image.open(path) as screenshot:
                    sx, sy = screenshot.width / bounds['width'], screenshot.height / bounds['height']
                    box = tuple(round(bounds[key] * scale) for key, scale in
                                [('left', sx), ('top', sy), ('right', sx), ('bottom', sy)])
                    if box[2] <= box[0] or box[3] <= box[1]:
                        raise RuntimeError('The listing form panel is outside the visible viewport')
                    path = output / f'form-{index:02d}.png'
                    screenshot.crop(box).save(path)
            lines, proposals = recognition_proposals(path, ocr, vision, warnings)
            print(f'[ListingImport][OCR] Recognized {len(lines)} text lines in {path.name}', flush=True)
            for key, proposal in proposals.items():
                proposal['screenshot'] = str(path.resolve())
                if key in fields and fields[key]['value'] != proposal['value']:
                    print(f'[ListingImport][OCR] CONFLICT {key}: {fields[key]["value"]!r} versus {proposal["value"]!r}; review required', flush=True)
                    fields[key]['state'] = 'conflict'
                    fields[key].setdefault('alternatives', []).append(proposal)
                else:
                    if key not in fields:
                        print(f'[ListingImport][OCR] READ {key}={proposal["value"]!r} confidence={proposal["confidence"]}; review required', flush=True)
                    fields.setdefault(key, proposal)
            captures.append({'path': str(path.resolve()), 'ocr_lines': lines})
            control_snapshot = read_edit_form_snapshot(driver)
            if isinstance(control_snapshot, dict):
                merge_control_fields(fields, control_snapshot, path.resolve())
                captures[-1]['control_snapshot'] = control_snapshot
            if include_media:
                images.extend(_safe_image_urls(driver))
                direct, missing = extract_video_sources(driver)
                videos.extend(direct)
                unresolved.extend(missing)
            print(f'[ListingImport][OCR] Media so far: {len(set(images))} photos, {len(set(videos))} direct videos, {len(unresolved)} unresolved video sources', flush=True)
            before, after, bottom = driver.execute_script("""
              const el=arguments[0], before=el.scrollTop;
              el.scrollTop+=Math.max(100,el.clientHeight*.65);
              return [before,el.scrollTop,el.scrollTop+el.clientHeight>=el.scrollHeight-2];
            """, scroll)
            if before == after:
                complete = bool(bottom)
                print(f'[ListingImport][OCR] Scrolling finished; bottom reached={complete}', flush=True)
                break
            print(f'[ListingImport][OCR] Scrolled from {before} to {after}', flush=True)
        if not complete:
            warnings.append('Capture limit reached or scrolling stalled; some fields may not be visible.')
        captures.extend(capture_description_scroll(driver, output))
    finally:
        driver.execute_script('arguments[0].scrollTop=arguments[1];', scroll, original)
        print('[ListingImport][OCR] Original scroll position restored', flush=True)
    output_fields = dict(EDIT_FIELDS, **ITEM_EDIT_FIELDS, **EXTRA_VEHICLE_FIELDS)
    post = {field: fields.get(field, {}).get('value', '') for field in output_fields}
    post.update(image_urls=list(dict.fromkeys(images)), video_urls=list(dict.fromkeys(videos)),
                field_states={field: fields.get(field, {}).get('state', 'unreadable') for field in output_fields})
    # Optional colours never determine whether the listing read succeeded.
    # 'not_detected' deliberately does not claim absence: detection may fail.
    for field in OPTIONAL_COLOR_FIELDS - fields.keys():
        post['field_states'][field] = 'not_detected'
    missing = [field for field in EDIT_FIELDS if field not in fields and field not in OPTIONAL_COLOR_FIELDS]
    if missing:
        warnings.append('Fields not detected (may be absent from this form): ' + ', '.join(missing))
    readable = [field for field in EDIT_FIELDS if str(post.get(field) or '').strip()
                and post['field_states'].get(field) in {'read', 'disabled'}]
    print(f'[ListingImport][OCR] Finished: {len(readable)}/{len(EDIT_FIELDS)} vehicle fields read; empty or unverified={", ".join(field for field in EDIT_FIELDS if field not in readable) or "none"}', flush=True)
    usable = bool(post['price']) and bool(post.get('title') or any(post[field] for field in ('year', 'make', 'model')))
    if not usable:
        warnings.append('Listing price and vehicle identity were not recovered; this read failed.')
    return {'ok': usable, 'experimental': True, 'requires_review': True, 'edit_url': driver.current_url,
            'posts': [post], 'fields': fields, 'captures': captures, 'video_unresolved': unresolved,
            'capture_complete': complete, 'warnings': warnings}


def read_import_edit_page(driver, *, require_usable=True, include_media=True):
    """Local import adapter: keep a report for each listing and read attempt."""
    import json
    from uuid import uuid4
    from automation.runtime_paths import data_dir
    output = data_dir('visual-listing-reader') / uuid4().hex[:12]
    result = read_listing_edit_visual(driver, output, allow_unstructured=True, max_captures=6, max_seconds=60, include_media=include_media)
    result['posts'][0]['visual_read_requires_review'] = True
    result['posts'][0]['visual_read_report'] = str((output / 'report.json').resolve())
    (output / 'report.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'[ListingImport][OCR] Review report: {output / "report.json"}', flush=True)
    if require_usable and not result['ok']:
        raise RuntimeError(f'Listing price or vehicle identity could not be read. Saved details were not replaced. Review {output / "report.json"}')
    return result


def read_import_fallback_page(driver, *, include_media=True):
    """Partial recovery may help even when the fallback cannot read a price."""
    return read_import_edit_page(driver, require_usable=False, include_media=include_media)
