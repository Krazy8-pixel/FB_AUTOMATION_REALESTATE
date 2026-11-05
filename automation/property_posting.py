"""Manual property posting through Facebook's mobile Marketplace form."""
from urllib.parse import urlsplit
import os
import re
import unicodedata
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from automation.account_browser import MOBILE_USER_AGENT, MOBILE_POSTING_URL
from automation.cdp_login import open_posting_tab, close_posting_tab
from automation.diagnostics import event as diagnostic_event
from automation.task_control import guard_driver, check_cancelled, cancellable_sleep

WAIT_SECONDS = 120
PHOTO_WAIT_SECONDS = 120
LOCATION_WAIT_SECONDS = 180
DEBUG_FIELD_DELAY_SECONDS = 5
DEBUG_ADD_PHOTOS_DELAY_SECONDS = 7


def _log(message):
    print('[Property Post] ' + message, flush=True)


def _debug_pause(message, seconds):
    _log(f'Debug pause: {message} ({seconds} seconds).')
    cancellable_sleep(seconds)


def _field(driver, label, name, tag='input'):
    """Find a field following its visible span; use stable data-name as fallback."""
    return driver.execute_script('''
      const label=arguments[0].toLowerCase(), name=arguments[1], tag=arguments[2];
      const shown=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
      const spans=[...document.querySelectorAll('span')].filter(e=>shown(e) &&
        (e.textContent||'').trim().toLowerCase().startsWith(label));
      for (const span of spans) {
        const area=span.closest('[data-mcomponent="ServerTextArea"]');
        if (area) {
          let next=area.nextElementSibling;
          for (let n=0;next && n<5;n++,next=next.nextElementSibling) {
            const f=next.matches(tag)?next:next.querySelector(tag);
            if (shown(f) && (!name || f.closest('[data-name]')?.getAttribute('data-name')===name)) return f;
          }
        }
        const lab=span.closest('label');
        if (lab) { const f=lab.control||lab.querySelector(tag); if(shown(f)) return f; }
      }
      const escaped=CSS.escape(name);
      const field=document.querySelector('[data-name="'+escaped+'"] '+tag+', '+tag+'[name="'+escaped+'"]');
      if (shown(field)) return field;
      return [...document.querySelectorAll(tag)].find(e=>shown(e) &&
        [e.placeholder,e.getAttribute('aria-label')].some(v=>v && v.toLowerCase().includes(label))) || null;
    ''', label, name, tag)


def _click_label(driver, labels):
    return driver.execute_script('''
      const texts=arguments[0].map(v=>v.toLowerCase());
      const visible=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
      const all=[...document.querySelectorAll('span'),...document.querySelectorAll('button,[role=button],a')];
      let node=null;
      for (const text of texts) {
        node=all.find(e=>visible(e) && (e.textContent||'').trim().toLowerCase()===text);
        if (node) break;
      }
      if(!node) return false;
      const target=node.closest('[role=button],[data-focusable=true],[data-action-id],button,a') || node;
      if(target.getAttribute('aria-disabled')==='true'||target.disabled) return false;
      target.scrollIntoView({block:'center'}); target.click(); return true;
    ''', labels)


def _label_visible(driver, labels):
    return driver.execute_script('''
      const texts=arguments[0].map(v=>v.toLowerCase());
      return [...document.querySelectorAll('span,button,[role=button]')].some(e=>
        e.getClientRects().length && texts.includes((e.textContent||'').trim().toLowerCase()));
    ''', labels)


def _set_input(driver, wait, label, name, value, tag='input'):
    element=wait.until(lambda d:_field(d,label,name,tag), 'Could not find '+label+' field.')
    element.click()
    element.send_keys(Keys.CONTROL, 'a')
    element.send_keys(str(value))
    if (element.get_attribute('value') or '').strip()!=str(value).strip():
        raise RuntimeError('Facebook did not retain the '+label+' value.')
    _debug_pause(f'{label} field entered', DEBUG_FIELD_DELAY_SECONDS)
    return element


_LOCATION_ALIASES = {
    'alabama':'al', 'alaska':'ak', 'arizona':'az', 'arkansas':'ar', 'california':'ca',
    'colorado':'co', 'connecticut':'ct', 'delaware':'de', 'florida':'fl', 'georgia':'ga',
    'hawaii':'hi', 'idaho':'id', 'illinois':'il', 'indiana':'in', 'iowa':'ia', 'kansas':'ks',
    'kentucky':'ky', 'louisiana':'la', 'maine':'me', 'maryland':'md', 'massachusetts':'ma',
    'michigan':'mi', 'minnesota':'mn', 'mississippi':'ms', 'missouri':'mo', 'montana':'mt',
    'nebraska':'ne', 'nevada':'nv', 'new hampshire':'nh', 'new jersey':'nj', 'new mexico':'nm',
    'new york':'ny', 'north carolina':'nc', 'north dakota':'nd', 'ohio':'oh', 'oklahoma':'ok',
    'oregon':'or', 'pennsylvania':'pa', 'rhode island':'ri', 'south carolina':'sc',
    'south dakota':'sd', 'tennessee':'tn', 'texas':'tx', 'utah':'ut', 'vermont':'vt',
    'virginia':'va', 'washington':'wa', 'west virginia':'wv', 'wisconsin':'wi', 'wyoming':'wy',
}
_LOCATION_COUNTRY_ALIASES = {
    'uk': 'united kingdom', 'britain': 'united kingdom', 'great britain': 'united kingdom',
    'us': 'united states', 'usa': 'united states', 'america': 'united states',
    'uae': 'united arab emirates', 'u.a.e': 'united arab emirates',
    'russia': 'russian federation', 'south korea': 'korea republic',
    'north korea': 'korea democratic people republic', 'czech republic': 'czechia',
    'ivory coast': 'cote divoire', 'holland': 'netherlands',
}
_LOCATION_COUNTRY_ALIAS_REVERSE = {
    alias: country for alias, country in _LOCATION_COUNTRY_ALIASES.items()
}
_LOCATION_ALIAS_REVERSE = {abbreviation: name for name, abbreviation in _LOCATION_ALIASES.items()}


def _location_text(value):
    normalized = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ''.join(char for char in normalized if not unicodedata.combining(char))


def _location_result_matches(requested, result):
    """Only accept a Facebook result that contains every requested location part."""
    requested_text = _location_text(requested)
    result_text = _location_text(result)
    requested_tokens = re.findall(r'[^\W_]+', requested_text, flags=re.UNICODE)
    result_tokens = re.findall(r'[^\W_]+', result_text, flags=re.UNICODE)
    if not requested_tokens or not result_tokens:
        return False
    result_compact = ''.join(result_tokens)
    requested_compact = ''.join(requested_tokens)
    if requested_compact in result_compact:
        return True
    result_set = set(result_tokens)
    requested_phrase = ' '.join(requested_tokens)
    aliases = {requested_phrase}
    if requested_phrase in _LOCATION_ALIASES:
        aliases.add(_LOCATION_ALIASES[requested_phrase])
    if len(requested_tokens) == 1 and requested_tokens[0] in _LOCATION_ALIAS_REVERSE:
        aliases.add(_LOCATION_ALIAS_REVERSE[requested_tokens[0]])
    if requested_phrase in _LOCATION_COUNTRY_ALIASES:
        aliases.add(_LOCATION_COUNTRY_ALIASES[requested_phrase])
    if requested_phrase in _LOCATION_COUNTRY_ALIAS_REVERSE:
        aliases.add(_LOCATION_COUNTRY_ALIAS_REVERSE[requested_phrase])
    if len(requested_tokens) > 1:
        country_suffix = ' '.join(requested_tokens[1:])
        if country_suffix in _LOCATION_COUNTRY_ALIASES:
            aliases.add(requested_tokens[0] + ' ' + _LOCATION_COUNTRY_ALIASES[country_suffix])
        if country_suffix in _LOCATION_COUNTRY_ALIAS_REVERSE:
            aliases.add(requested_tokens[0] + ' ' + _LOCATION_COUNTRY_ALIAS_REVERSE[country_suffix])
    if not any(
        alias in result_set or ''.join(alias.split()) in result_compact
        for alias in aliases
    ):
        return False
    return True


def _select_location(driver, wait, location):
    requested = str(location or '').strip()
    if not requested:
        raise ValueError('Location is required.')
    _log('Clicking the Location field on the main listing form.')
    entry=wait.until(lambda d:_field(d,'location','location'), 'Could not find Location field.')
    entry.click()
    search=wait.until(lambda d:d.execute_script('''
      const shown=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
      return [...document.querySelectorAll('[data-name="location_query"] input, input[placeholder="Location"]')]
        .find(e=>shown(e)&&!e.disabled)||null;
    '''), 'Facebook location search page did not open.')
    _log('Separate location search page opened. Replacing search text with: '+requested)
    search.click()
    search.send_keys(Keys.CONTROL, 'a')
    search.send_keys(requested)
    query_wait = WebDriverWait(driver, LOCATION_WAIT_SECONDS, poll_frequency=0.5)
    query_wait.until(lambda d:d.execute_script('''
      const input=document.querySelector('[data-name="location_query"] input, input[placeholder="Location"]');
      return !!input && (input.value||'').trim().toLowerCase()===arguments[0].trim().toLowerCase();
    ''', requested), 'Facebook did not retain the requested location search text.')
    _log('Search text is present. Pressing Enter to make Facebook search for it.')
    search.send_keys(Keys.ENTER)
    _log(f'Enter submitted the location search. Waiting for refreshed results (up to {LOCATION_WAIT_SECONDS} seconds).')

    def first_search_result(d):
        return d.execute_script('''
          const requested=arguments[0];
          const normalize=s=>(s||'').normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase();
          const input=document.querySelector('[data-name="location_query"] input, input[placeholder="Location"]');
          if(!input || normalize(input.value).trim()!==normalize(requested).trim()) return false;
          const visible=e=>!!(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
          const bottom=input.getBoundingClientRect().bottom;
          const nodes=[...document.querySelectorAll('[data-action-id],[role="option"],[role="button"],button,a,[data-focusable="true"]')];
          const candidates=[];
          for(const e of nodes){
            if(!visible(e) || e===input || e.contains(input)) continue;
            const rect=e.getBoundingClientRect();
            if(rect.top<bottom-3) continue;
            const text=(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
            if(!text || /search on facebook|see more|no results/i.test(text)) continue;
            if(candidates.some(parent=>parent.contains(e))) continue;
            for(let i=candidates.length-1;i>=0;i--) if(e.contains(candidates[i])) candidates.splice(i,1);
            candidates.push(e);
          }
          const first=candidates[0];
          return first && (first.closest('[data-action-id]')||first.closest('[role="option"],[role="button"],button,a')||first);
        ''', requested)

    try:
        selected_result = query_wait.until(
            first_search_result,
            f'Facebook did not show a location result for: {requested}.'
        )
    except TimeoutException as exc:
        visible_results = driver.execute_script('''
          const input=document.querySelector('[data-name="location_query"] input, input[placeholder="Location"]');
          if(!input) return [];
          const bottom=input.getBoundingClientRect().bottom;
          return [...document.querySelectorAll('[role="option"],[role="button"],button,a,[data-action-id],[data-focusable="true"]')]
            .filter(e=>e.getClientRects().length && e.getBoundingClientRect().top>=bottom-3)
            .map(e=>(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim())
            .filter(Boolean).slice(0,8);
        ''') or []
        summary = '; '.join(str(value) for value in visible_results)
        detail = f'Facebook did not return any location results for "{requested}".'
        if summary:
            detail += ' Visible results: ' + summary
        raise RuntimeError(detail + ' No location was selected; posting stopped before Publish.') from exc

    selected_result_text = (selected_result.text or '').strip()
    _log('Selecting the first Facebook location result: '+selected_result_text)
    diagnostic_event('PROPERTY_LOCATION', 'RESULT_SELECTED', requested=requested, result=selected_result_text)
    selected_result.click()
    _log('Waiting for Facebook to return to the main listing form.')
    wait.until(lambda d:_field(d,'what are you selling','title'), 'Facebook did not return to the listing form.')
    selected=wait.until(lambda d:_field(d,'location','location'), 'Location field did not return.')
    selected_value=(selected.get_attribute('value') or '').strip()
    if not selected_value:
        raise RuntimeError('Facebook returned to the listing form without retaining the selected location.')
    _log('Verified Facebook retained the selected location: '+selected_value)
    diagnostic_event('PROPERTY_LOCATION', 'RETAINED', selected=selected_value)
    _debug_pause('Location field selected', DEBUG_FIELD_DELAY_SECONDS)


def _upload_photos(driver, wait, images):
    photo_wait = WebDriverWait(driver, PHOTO_WAIT_SECONDS, poll_frequency=0.5)

    def main_form(d):
        return bool(_field(d, 'what are you selling', 'title') and
                    _label_visible(d, ['Add Photos', 'Add Photo']))

    def file_input(d):
        fields = [element for element in d.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                  if 'video' not in (element.get_attribute('accept') or '').lower()]
        return fields[-1] if fields else None

    def block_native_picker():
        driver.execute_script('''
          const proto=HTMLInputElement.prototype;
          if(!proto.__fbaOriginalClick){
            Object.defineProperty(proto,'__fbaOriginalClick',{value:proto.click,configurable:true});
            proto.click=function(){
              if(this.type==='file'){window.__fbaPendingFileInput=this;return;}
              return proto.__fbaOriginalClick.apply(this,arguments);
            };
          }
        ''')

    def restore_native_picker():
        driver.execute_script('''
          const proto=HTMLInputElement.prototype;
          if(proto.__fbaOriginalClick){
            proto.click=proto.__fbaOriginalClick;
            delete proto.__fbaOriginalClick;
          }
          delete window.__fbaPendingFileInput;
        ''')

    def send_file_without_dialog(upload, path):
        driver.execute_script('''
          const e=arguments[0];
          e.dataset.fbaOriginalStyle=e.getAttribute('style')||'';
          e.style.setProperty('display','block','important');
          e.style.setProperty('visibility','visible','important');
          e.style.setProperty('opacity','0.01','important');
          e.style.setProperty('position','fixed','important');
          e.style.setProperty('left','0','important');
          e.style.setProperty('top','0','important');
          e.style.setProperty('width','1px','important');
          e.style.setProperty('height','1px','important');
        ''', upload)
        try:
            upload.send_keys(path)
        finally:
            driver.execute_script('''
              const e=arguments[0], style=e.dataset.fbaOriginalStyle;
              if(style) e.setAttribute('style',style); else e.removeAttribute('style');
              delete e.dataset.fbaOriginalStyle;
            ''', upload)

    for index, path in enumerate(images, 1):
        check_cancelled()
        _log(f'Photo {index}/{len(images)}: opening Add Photos.')
        wait.until(lambda d: _click_label(d, ['Add Photos', 'Add Photo']),
                   'Could not open Add Photos.')
        _debug_pause('Add Photos opened', DEBUG_ADD_PHOTOS_DELAY_SECONDS)
        _log(f'Photo {index}/{len(images)}: choosing Upload Photo with the native picker suppressed.')
        block_native_picker()
        try:
            wait.until(lambda d: _click_label(d, ['Upload Photo']),
                       'Could not choose Upload Photo.')
            upload = wait.until(file_input, 'Facebook did not expose a photo upload field.')
        finally:
            restore_native_picker()
        _log(f'Photo {index}/{len(images)}: setting file directly on the browser upload control.')
        send_file_without_dialog(upload, path)
        _log(f'Photo {index}/{len(images)}: waiting for preview and Save (up to {PHOTO_WAIT_SECONDS}s).')
        photo_wait.until(lambda d: _label_visible(d, ['Save']),
                         f'Facebook did not open the preview for photo {index}.')
        _log(f'Photo {index}/{len(images)}: clicking Save.')
        photo_wait.until(lambda d: _click_label(d, ['Save']),
                         f'Could not click Save for photo {index}.')
        _log(f'Photo {index}/{len(images)}: waiting for upload to finish and form to return.')
        photo_wait.until(main_form,
                         f'Facebook did not return to the listing form after photo {index}.')
        _log(f'Photo {index}/{len(images)}: Facebook returned to the listing form.')
        _debug_pause(f'Photo {index} field saved', DEBUG_FIELD_DELAY_SECONDS)
    _log(f'All {len(images)} photo(s) completed the Preview and Save flow.')
    diagnostic_event('PROPERTY_MEDIA', 'UPLOADED', photos=len(images))


def _select_requested_groups(driver, group_names):
    targets = [str(name or '').strip() for name in (group_names or []) if str(name or '').strip()]
    _log(
        f'Waiting for Facebook group selection page; requested groups: {len(targets)}.'
        if targets else
        'Waiting for Facebook group selection page to clear any default group selections.'
    )
    wait = WebDriverWait(driver, WAIT_SECONDS, poll_frequency=0.5)
    try:
        wait.until(lambda d: _label_visible(d, ['Share']) or d.execute_script(
            "return !!document.querySelector('[role=checkbox][data-name=\"groups[]\"]')"),
            'Facebook did not open the group selection page.')
    except Exception as exc:
        _log(f'Could not confirm group selection screen; continuing to Share: {exc}')
        return

    matched = set()
    rounds = 0
    max_rounds = min(WAIT_SECONDS * 2, 30) if targets else 1
    while rounds < max_rounds and (len(matched) < len(targets) or not targets):
        rounds += 1
        try:
            result = driver.execute_script('''
          const targets=arguments[0];
          const norm=s=>(s||'').toLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,' ').trim();
          const boxes=[...document.querySelectorAll('[role="checkbox"][data-name="groups[]"]')]
            .filter(e=>e.getClientRects().length);
          const found=[], cleared=[];
          for(const box of boxes){
            let row=box.parentElement;
            const rowText=norm(row?.innerText||row?.textContent||'');
            const selected=(box.getAttribute('aria-checked')||'').toLowerCase()==='true';
            const target=targets.find(value=>{
              const key=norm(value);
              return key && rowText.includes(key);
            });
            if(selected && !target){ box.click(); cleared.push(rowText); continue; }
            if(!target) continue;
            if(!selected) box.click();
            if(!found.includes(target)) found.push(target);
          }
          const scrollers=[...document.querySelectorAll('*')].filter(e=>{
            const s=getComputedStyle(e);
            return (s.overflowY==='auto'||s.overflowY==='scroll') && e.scrollHeight>e.clientHeight+20;
          });
          const scroller=scrollers.sort((a,b)=>b.clientHeight-a.clientHeight)[0];
          if(scroller) scroller.scrollTop+=Math.max(200,scroller.clientHeight-80);
          else window.scrollBy(0,Math.max(300,window.innerHeight-100));
          return {found,cleared:cleared.length,boxes:boxes.length};
            ''', targets) or {}
        except Exception as exc:
            _log(f'Group selection attempt failed; continuing to Share: {exc}')
            break
        found_now = result.get('found') or []
        for name in found_now:
            matched.add(name)
            _log(f'Selected requested Facebook group: {name}')
        if result.get('cleared'):
            _log(f'Cleared {result["cleared"]} unrequested selected Facebook group(s).')
        if not targets or len(matched) >= len(targets) or not result.get('boxes'):
            break
        if len(matched) < len(targets):
            check_cancelled()
            cancellable_sleep(0.5)

    missing = [name for name in targets if name not in matched]
    for name in missing:
        _log(f'Requested group was not present on Facebook; skipping it: {name}')
    _log(f'Group selection complete: {len(matched)} selected, {len(missing)} skipped.')


def _clear_unrequested_group_selections(driver, group_names):
    targets = [str(name or '').strip() for name in (group_names or []) if str(name or '').strip()]
    try:
        result = driver.execute_script('''
          const targets=arguments[0];
          const norm=s=>(s||'').toLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,' ').trim();
          let cleared=0;
          for(const box of [...document.querySelectorAll('[role="checkbox"][data-name="groups[]"]')]){
            if(!box.getClientRects().length || (box.getAttribute('aria-checked')||'').toLowerCase()!=='true') continue;
            const row=box.parentElement;
            const text=norm(row?.innerText||row?.textContent||'');
            const requested=targets.some(value=>{const key=norm(value);return key && text.includes(key);});
            if(!requested){box.click();cleared++;}
          }
          return cleared;
        ''', targets)
        if result:
            _log(f'Final safety pass cleared {result} unrequested group selection(s).')
    except Exception as exc:
        _log(f'Final group selection safety pass could not complete: {exc}')


def _click_final_share(driver, group_names):
    try:
        _select_requested_groups(driver, group_names)
    except Exception as exc:
        _log(f'Group selection failed; skipping remaining group work and moving to Share: {exc}')
    _clear_unrequested_group_selections(driver, group_names)
    wait = WebDriverWait(driver, WAIT_SECONDS, poll_frequency=0.5)
    _log('Clicking final Share on Facebook.')
    wait.until(lambda d: _click_label(d, ['Share']), 'Facebook Share button was not available.')
    diagnostic_event('PROPERTY_POST', 'SHARE_CLICKED')
    _log(f'Final Share was clicked; waiting for Facebook to finish (up to {WAIT_SECONDS} seconds).')
    try:
        wait.until(lambda d: not _label_visible(d, ['Share']) or d.execute_script('''
          const text=(document.body.innerText||'').toLowerCase();
          return /shared|posted|listing is live|published|under review|being reviewed/.test(text) ||
                 location.pathname.includes('/marketplace/item/');
        '''))
        _log('Facebook completed the final Share step.')
    except TimeoutException:
        _log('Share was clicked, but Facebook gave no clear completion signal; treating the submission as sent.')


def post_property(data, account):
    images = [os.path.abspath(data['image_path%d' % index])
              for index in range(1, 21) if data.get('image_path%d' % index)]
    if not images:
        raise ValueError('No photo paths were attached to the property post.')
    missing_images = [path for path in images if not os.path.isfile(path)]
    if missing_images:
        for path in missing_images:
            _log('Selected photo file is missing when posting starts: ' + path)
        raise ValueError(f'{len(missing_images)} selected photo file(s) are missing when posting starts.')
    _log(f'Validated {len(images)} persistent photo file(s) for upload.')
    for key in ('title','price','location'):
        if not str(data.get(key) or '').strip(): raise ValueError(key.title()+' is required.')
    diagnostic_event('PROPERTY_POST', 'START', account=account, title=data.get('title'),
                     location=data.get('location'), photos=len(images))
    stage = 'open_browser'
    _log('Starting mobile property post for account: '+account)
    driver, handle, original, lock=open_posting_tab(account)
    submitted=False
    try:
        guard_driver(driver)
        driver.set_page_load_timeout(WAIT_SECONDS)
        driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width':412,'height':800,'deviceScaleFactor':2.625,'mobile':True})
        driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {'userAgent':MOBILE_USER_AGENT,'platform':'Android'})
        _log('Opening Facebook mobile listing form.')
        driver.get(MOBILE_POSTING_URL)
        stage = 'listing_form'
        wait=WebDriverWait(driver, WAIT_SECONDS, poll_frequency=0.5)
        try:
            wait.until(lambda d:_field(d,'what are you selling','title'))
        except TimeoutException as exc:
            page=urlsplit(driver.current_url or '')
            raise RuntimeError('Could not find listing form on '+page.netloc+page.path+'. Check login and Marketplace access.') from exc
        _log('Selecting category: Home sales / Property for sale.')
        wait.until(lambda d:_click_label(d,['Select','Category']), 'Could not open category selector.')
        wait.until(lambda d:_click_label(d,['Home sales','Property for sale','Home for sale']), 'Property category not found.')
        wait.until(lambda d:_field(d,'what are you selling','title'), 'Listing form did not return after category selection.')
        if not driver.execute_script("return /home sales|property for sale|home for sale/i.test(document.body.innerText || '')"):
            raise RuntimeError('Facebook did not confirm the selected property category.')
        diagnostic_event('PROPERTY_POST', 'CATEGORY_SELECTED', category='home_sales')
        stage = 'listing_fields'
        _log('Entering title.')
        _set_input(driver,wait,'what are you selling','title',data['title'])
        _log('Entering price.')
        _set_input(driver,wait,'price','price',data['price'])
    _log('Searching location and waiting for Facebook results.')
        stage = 'location'
        _select_location(driver,wait,data['location'])
        _log('Location selected. Entering description.')
        if data.get('description'): _set_input(driver,wait,'description','description',data['description'],'textarea')
        stage = 'photos'
        diagnostic_event('PROPERTY_MEDIA', 'UPLOAD_STARTED', account=account, photos=len(images))
        _upload_photos(driver, wait, images)
        check_cancelled()
        _log('Clicking Publish on the completed property form.')
        stage = 'publish'
        submitted=True
        wait.until(lambda d:_click_label(d,['Publish']), 'Facebook Publish button was not available.')
        diagnostic_event('PROPERTY_POST', 'PUBLISH_CLICKED', account=account, title=data.get('title'))
        _log('Publish clicked; Facebook is opening the group selection page.')
        _click_final_share(driver, data.get('group_names') or [])
        data['_confirmed_listing_url']=driver.current_url if '/marketplace/item/' in driver.current_url else ''
        _log('Property post flow completed through Facebook Share.')
        diagnostic_event('PROPERTY_POST', 'COMPLETED', account=account, title=data.get('title'))
        return True
    except Exception as exc:
        diagnostic_event('PROPERTY_POST', 'FAILED', account=account, title=data.get('title'),
                         stage=stage, publish_clicked=submitted, error_type=type(exc).__name__, error=str(exc))
        if submitted:
            _log('Publish status is uncertain. Check Facebook before retrying.')
            raise RuntimeError('Publish may have been submitted. Check Facebook before retrying. '+str(exc)) from exc
        _log('Stopped before publishing: '+str(exc))
        raise
    finally:
        _log('Closing the dedicated Facebook posting browser.')
        browser_closed = close_posting_tab(
            driver, handle, original, lock, close_browser=True, verify_browser_closed=True
        )
        diagnostic_event('PROPERTY_POST', 'BROWSER_CLOSED' if browser_closed else 'BROWSER_CLOSE_UNVERIFIED',
                         account=account, title=data.get('title'))
