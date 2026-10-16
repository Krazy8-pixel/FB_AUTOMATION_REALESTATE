(() => {
  const page = document.querySelector('.listing-detail');
  if (!page) return;
  const form = document.getElementById('repost-form');
  const readView = document.getElementById('listing-read-view');
  const start = document.getElementById('start-repost');
  const crossStart = document.getElementById('start-cross-post');
  start.after(crossStart);
  const crossAccounts = document.getElementById('cross-post-accounts');
  let crossPosting = false;
  const workflowHelp = document.getElementById('posting-workflow-help');
  const repostHelp = workflowHelp.textContent;
  const deleteButton = document.getElementById('start-delete');
  const deleteOptions = document.getElementById('delete-options');
  let deleteJob = null;
  let deleting = false;
  const feedback = document.getElementById('workflow-status');
  const order = document.getElementById('photo-order');
  const initialOrder = [...order.children].map(row => row.dataset.photo);
  const submit = document.getElementById('confirm-repost');
  const delay = document.getElementById('repost-delay-value');
  const unit = document.getElementById('repost-delay-unit');
  const schedule = document.getElementById('schedule-fields');
  let submitting = false;
  function message(text, error=false) { feedback.textContent = text; feedback.classList.toggle('is-error', error); }
  function refreshOrder() {
    [...order.children].forEach((row, index, rows) => {
      row.querySelector('.ld-order-label').textContent = index === 0 ? 'Cover photo' : `Photo ${index + 1}`;
      row.querySelector('[data-move="first"]').disabled = index === 0;
      row.querySelector('[data-move="up"]').disabled = index === 0;
      row.querySelector('[data-move="down"]').disabled = index === rows.length - 1;
      row.querySelector('.ld-drag-handle').setAttribute('aria-label', `Drag photo ${index + 1} to reorder`);
    });
  }
  function timing() {
    const later = form.elements.post_mode.value === 'schedule';
    schedule.hidden = !later;
    delay.required = later;
    delay.max = unit.value === 'days' ? 365 : unit.value === 'hours' ? 24 : 59;
    submit.textContent = crossPosting ? (later ? 'Schedule cross posting' : 'Post now') : (later ? 'Schedule repost' : 'Repost now');
  }
  function exit() { form.hidden = true; readView.hidden = false; start.hidden = false; crossStart.hidden = false; deleteButton.hidden = false; }
  function hideDeleteOptions() { deleteOptions.hidden = true; deleteButton.setAttribute('aria-expanded', 'false'); }
  deleteButton.addEventListener('click', () => {
    if (deleting) return;
    deleteOptions.hidden = !deleteOptions.hidden;
    deleteButton.setAttribute('aria-expanded', String(!deleteOptions.hidden));
  });
  document.getElementById('cancel-delete').addEventListener('click', hideDeleteOptions);
  deleteOptions.addEventListener('click', async event => {
    const choice = event.target.closest('[data-delete-target]');
    if (!choice || deleting) return;
    deleting = true; hideDeleteOptions();
    deleteButton.textContent = 'Deleting…'; deleteButton.disabled = true; start.disabled = true; message('');
    try {
      const response = await fetch(page.dataset.deleteUrl, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ids:[page.dataset.listingId], target:choice.dataset.deleteTarget, account:page.dataset.account})});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Deletion could not start.');
      if (result.completed) { window.location.assign(page.dataset.listingsUrl); return; }
      deleteJob = result.job_id;
      await poll(false);
    } catch (error) {
      deleting = false; deleteJob = null; deleteButton.textContent = 'Delete listing'; deleteButton.disabled = false;
      message(error.message || 'Could not reach the app. Try again.', true); await poll(false);
    }
  });
  function openPosting(cross) {
    crossPosting = cross;
    document.getElementById('posting-timing-label').textContent = cross ? 'Posting timing' : 'Repost timing';
    crossAccounts.hidden = !cross;
    workflowHelp.textContent = cross ? 'Your original listing stays unchanged. The delay sets when this queue begins. Each account is posted to in turn, even if an earlier attempt fails. Track results in Scheduled Posts.' : repostHelp;
    form.querySelector('.ld-eyebrow').textContent = cross ? 'POST ON OTHER ACCOUNTS' : 'PREPARE YOUR REPOST';
    form.querySelector('.ld-timing-options label').lastChild.textContent = cross ? ' Post now' : ' Repost now';
    crossStart.hidden = true;
    hideDeleteOptions(); deleteButton.hidden = true;
    form.hidden = false; readView.hidden = true; start.hidden = true; message(''); refreshOrder(); timing();
    if (cross) crossAccounts.scrollIntoView({block:'start', behavior:'smooth'});
    else form.querySelector('input[name="title"]').focus();
  }
  start.addEventListener('click', () => openPosting(false));
  crossStart.addEventListener('click', () => openPosting(true));
  document.getElementById('cross-select-all').addEventListener('click', () => {
    const boxes = [...crossAccounts.querySelectorAll('[data-cross-account]')];
    const selected = boxes.filter(box => box.checked).length;
    boxes.forEach((box, index) => { box.checked = selected < Math.min(19, boxes.length) && index < 19; });
  });
  document.getElementById('cancel-repost').addEventListener('click', () => {
    if (submitting) return;
    finishDrag();
    form.reset(); initialOrder.forEach(name => { const row = [...order.children].find(item => item.dataset.photo === name); if (row) order.append(row); });
    exit(); message('');
  });
  document.querySelectorAll('[data-preview]').forEach(button => button.addEventListener('click', () => {
    document.getElementById('listing-hero').src = button.dataset.preview;
    document.querySelectorAll('[data-preview]').forEach(item => item.classList.toggle('is-active', item === button));
  }));
  order.addEventListener('click', event => {
    const button = event.target.closest('[data-move]');
    if (!button || button.disabled) return;
    const row = button.closest('[data-photo]');
    if (button.dataset.move === 'first') order.prepend(row);
    if (button.dataset.move === 'up' && row.previousElementSibling) order.insertBefore(row, row.previousElementSibling);
    if (button.dataset.move === 'down' && row.nextElementSibling) order.insertBefore(row.nextElementSibling, row);
    refreshOrder();
  });
  let draggedPhoto = null;
  let dropTarget = null;
  let dropAfter = false;
  let pointerDrag = null;
  let dragPreview = null;
  let scrollFrame = null;
  function clearDropTarget() {
    order.querySelectorAll('.is-drop-before, .is-drop-after').forEach(row => row.classList.remove('is-drop-before', 'is-drop-after'));
    dropTarget = null;
  }
  function markDropTarget(x, y) {
    clearDropTarget();
    const bounds = order.getBoundingClientRect();
    if (x < bounds.left || x > bounds.right || y < bounds.top || y > bounds.bottom) return;
    let target = document.elementFromPoint(x, y)?.closest('[data-photo]');
    if (target === draggedPhoto) return;
    if (!target || !order.contains(target)) {
      // Include grid gaps so users can drop between rows as well as on photos.
      let closest = Infinity;
      for (const row of order.children) {
        if (row === draggedPhoto) continue;
        const box = row.getBoundingClientRect();
        const distance = Math.max(box.left-x, 0, x-box.right)**2 + Math.max(box.top-y, 0, y-box.bottom)**2;
        if (distance < closest) { closest = distance; target = row; }
      }
    }
    if (!target) return;
    const box = target.getBoundingClientRect();
    dropAfter = y > box.bottom-box.height*.25 || (y >= box.top+box.height*.25 && x > box.left+box.width/2);
    dropTarget = target;
    target.classList.add(dropAfter ? 'is-drop-after' : 'is-drop-before');
  }
  function finishDrag(commit=false) {
    if (commit && draggedPhoto && dropTarget && !submitting) {
      order.insertBefore(draggedPhoto, dropAfter ? dropTarget.nextElementSibling : dropTarget);
      refreshOrder();
    }
    draggedPhoto?.classList.remove('is-dragging');
    draggedPhoto = null; clearDropTarget();
    dragPreview?.remove(); dragPreview = null;
    if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
    scrollFrame = null;
    const previousPointer = pointerDrag; pointerDrag = null;
    if (previousPointer?.handle.hasPointerCapture(previousPointer.id)) previousPointer.handle.releasePointerCapture(previousPointer.id);
  }
  order.addEventListener('dragstart', event => {
    const image = event.target.closest('.ld-order-image');
    if (!image || event.target.closest('.ld-drag-handle') || submitting || pointerDrag) { event.preventDefault(); return; }
    draggedPhoto = image.closest('[data-photo]');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', draggedPhoto.dataset.photo);
    draggedPhoto.classList.add('is-dragging');
  });
  order.addEventListener('dragover', event => {
    if (!draggedPhoto || pointerDrag || submitting) return;
    event.preventDefault(); event.dataTransfer.dropEffect = 'move'; markDropTarget(event.clientX, event.clientY);
  });
  order.addEventListener('drop', event => {
    if (!draggedPhoto || pointerDrag) return;
    event.preventDefault(); markDropTarget(event.clientX, event.clientY); finishDrag(true);
  });
  order.addEventListener('dragleave', event => { if (!order.contains(event.relatedTarget)) clearDropTarget(); });
  order.addEventListener('dragend', () => finishDrag());
  document.addEventListener('dragover', event => {
    if (draggedPhoto && !pointerDrag && !order.contains(event.target)) { event.preventDefault(); clearDropTarget(); }
  });
  document.addEventListener('drop', event => {
    if (draggedPhoto && !pointerDrag && !order.contains(event.target)) { event.preventDefault(); finishDrag(); }
  });
  function scrollWhileDragging() {
    if (!pointerDrag?.active) return;
    const y = pointerDrag.y;
    const speed = y < 65 ? -Math.min(16, (65-y)/4) : y > innerHeight-65 ? Math.min(16, (y-innerHeight+65)/4) : 0;
    if (speed) { window.scrollBy(0, speed); markDropTarget(pointerDrag.x, y); }
    scrollFrame = requestAnimationFrame(scrollWhileDragging);
  }
  order.addEventListener('pointerdown', event => {
    const handle = event.target.closest('.ld-drag-handle');
    if (!handle || event.button !== 0 || !event.isPrimary || submitting) return;
    event.preventDefault(); finishDrag();
    pointerDrag = {id:event.pointerId, handle, photo:handle.closest('[data-photo]'), startX:event.clientX, startY:event.clientY, x:event.clientX, y:event.clientY, active:false};
    handle.setPointerCapture(event.pointerId);
  });
  order.addEventListener('pointermove', event => {
    if (!pointerDrag || pointerDrag.id !== event.pointerId) return;
    pointerDrag.x = event.clientX; pointerDrag.y = event.clientY;
    if (!pointerDrag.active && Math.hypot(event.clientX-pointerDrag.startX, event.clientY-pointerDrag.startY) < 6) return;
    event.preventDefault();
    if (!pointerDrag.active) {
      pointerDrag.active = true; draggedPhoto = pointerDrag.photo; draggedPhoto.classList.add('is-dragging');
      dragPreview = draggedPhoto.querySelector('img').cloneNode();
      dragPreview.className = 'ld-drag-preview'; dragPreview.alt = ''; dragPreview.setAttribute('aria-hidden', 'true');
      document.body.append(dragPreview); scrollFrame = requestAnimationFrame(scrollWhileDragging);
    }
    dragPreview.style.left = `${event.clientX+12}px`; dragPreview.style.top = `${event.clientY+12}px`;
    markDropTarget(event.clientX, event.clientY);
  });
  order.addEventListener('pointerup', event => {
    if (pointerDrag?.id !== event.pointerId) return;
    if (pointerDrag.active) markDropTarget(event.clientX, event.clientY);
    finishDrag(true);
  });
  order.addEventListener('pointercancel', event => { if (pointerDrag?.id === event.pointerId) finishDrag(); });
  order.addEventListener('lostpointercapture', event => { if (pointerDrag?.id === event.pointerId) finishDrag(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') finishDrag(); });
  form.addEventListener('change', timing);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    finishDrag();
    if (!form.reportValidity()) return;
    const postMode = form.elements.post_mode.value;
    if (postMode === 'schedule' && (!Number.isInteger(Number(delay.value)) || Number(delay.value)<1 || Number(delay.value)>Number(delay.max))) {
      message(`Enter a delay from 1 to ${delay.max}.`, true); return;
    }
    const fields = Object.fromEntries(new FormData(form)); delete fields.post_mode;
    const payload = {account:page.dataset.account, listing_id:page.dataset.listingId, fields, image_order:[...order.children].map(row=>row.dataset.photo), post_mode:postMode, delay_value:Number(delay.value), delay_unit:unit.value};
    if (crossPosting) {
      payload.target_accounts = [...crossAccounts.querySelectorAll('[data-cross-account]:checked')].map(box => box.value);
      if (!payload.target_accounts.length || payload.target_accounts.length >= 20) {
        message('Select 1 to 19 other accounts.', true); return;
      }
    }
    submitting = true; document.getElementById('repost-fields').disabled = true;
    message(crossPosting ? 'Queuing cross posting...' : 'Submitting your repost...');
    try {
      const response = await fetch(crossPosting ? crossAccounts.dataset.url : page.dataset.repostUrl, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok || !result.ok) { message(result.message || 'Could not start the repost.', true); return; }
      exit(); if (!crossPosting) start.disabled = true;
      message(result.message + (postMode === 'schedule' ? ` Runs at ${result.run_at}.` : ''));
      if (!crossPosting) {
        const badge = document.getElementById('listing-status'); badge.hidden=false; badge.textContent = postMode === 'schedule' ? 'Scheduled' : 'Preparing repost';
      }
    } catch (error) { message('Could not reach the app. Try again.', true); }
    finally { submitting=false; document.getElementById('repost-fields').disabled=false; timing(); }
  });
  const labels = {queued:'Queued', pending:'Scheduled', reading:'Preparing repost', posting:'Reposting', deleting:'Deleting original listing', done:'Reposted', failed:'Repost failed', facebook_deleting:'Deleting from Facebook'};
  async function poll(scheduleNext=true) {
    try {
      const response = await fetch(page.dataset.statusUrl); const data = await response.json();
      if (data.account !== page.dataset.account) {
        start.disabled = true; crossStart.disabled = true; deleteButton.disabled = true; hideDeleteOptions();
        message('The active account changed. Return to All listings to continue.', true);
        return;
      }
      const deletion = data.facebook_delete_status || {};
      if (deletion.ids?.includes(page.dataset.listingId)) {
        if (deletion.running) { deleting = true; deleteJob = deletion.job_id; hideDeleteOptions(); }
        else if (deleteJob === deletion.job_id) {
          deleting = false; deleteJob = null;
          if (deletion.removed_ids?.includes(page.dataset.listingId)) { window.location.assign(page.dataset.listingsUrl); return; }
          message(deletion.error || deletion.message, !!deletion.error);
        }
      }
      const item = (data.listings || []).find(item => item.id === page.dataset.listingId);
      if (item) {
        const facebookBadge = document.getElementById('listing-facebook-status');
        facebookBadge.hidden = item.facebook_present === false || (item.source_deleted && !item.facebook_reposted);
        facebookBadge.textContent = 'Posted on Facebook';
        const state = item.display_status || '';
        const badge = document.getElementById('listing-status'); badge.textContent=labels[state] || ''; badge.hidden=!labels[state];
        const busy = deleting || (data.scheduled_repost_ids || []).includes(page.dataset.listingId) || ['queued','pending','reading','posting','deleting','facebook_deleting'].includes(state) || !!data.import_status?.running || !!data.click_refresh_status?.running || !!deletion.running;
        start.disabled = busy || !item.details || !item.downloaded_images?.length || !!item.image_errors?.length;
        crossStart.disabled = start.disabled || !crossAccounts.querySelector('[data-cross-account]');
        deleteButton.disabled = busy;
        deleteButton.textContent = deleting ? 'Deleting…' : 'Delete listing';
      } else if (!deleting) {
        window.location.assign(page.dataset.listingsUrl); return;
      }
    } catch (error) {}
    finally { if (scheduleNext) setTimeout(poll, 5000); }
  }
  refreshOrder(); timing(); setTimeout(poll, 5000);
})();
