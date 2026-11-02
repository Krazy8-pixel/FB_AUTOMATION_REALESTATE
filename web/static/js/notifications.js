(() => {
  const queue = [];
  let current = null;
  let timer = null;

  function showNext() {
    if (current || !queue.length) return;
    const {text, kind} = queue.shift();
    const box = document.createElement('div');
    box.className = 'message-box';
    box.setAttribute('role', kind === 'failed' ? 'alert' : 'status');
    box.setAttribute('aria-live', kind === 'failed' ? 'assertive' : 'polite');
    const close = document.createElement('button');
    close.type = 'button'; close.className = 'close-msg'; close.setAttribute('aria-label', 'Close message');
    close.textContent = '×';
    const content = document.createElement('div');
    content.className = kind === 'failed' ? 'message-failed' : kind === 'success' ? 'message-success' : 'message-loading';
    content.textContent = text;
    const dismiss = () => { clearTimeout(timer); box.remove(); current = null; showNext(); };
    close.addEventListener('click', dismiss);
    box.append(close, content); document.body.append(box); current = box;
    timer = setTimeout(dismiss, kind === 'failed' ? 10000 : 7000);
  }

  window.appNotify = (message, kind = 'info') => {
    const text = String(message || '').trim();
    if (!text || (current && current.lastChild.textContent === text) || queue.some(item => item.text === text)) return;
    queue.push({text, kind}); showNext();
  };
  // Existing one-button alerts become app notifications. Confirmations keep
  // their normal yes/no behavior so no destructive action happens silently.
  window.alert = message => window.appNotify(message, 'failed');

  const selectors = '#workflow-status, #listing-feedback, #manage-cross-error, #clear-results-message, #session-check-text, [data-active-task-message], .header-mode-message';
  const seen = new WeakMap();
  function inspect(node) {
    const text = (node.textContent || '').trim();
    if (!text) { seen.set(node, ''); return; }
    if (seen.get(node) === text) return;
    seen.set(node, text);
    // Live counters and progress remain on the page, without repeated popups.
    if (/^(checking|clearing|importing|deleting|refreshing|starting|stopping|saving|queuing|submitting)\b/i.test(text)) return;
    const failed = node.classList.contains('is-error') || /\b(failed|could not|cannot|can't|error|select|choose|add and save|try again|missing)\b/i.test(text);
    window.appNotify(text, failed ? 'failed' : 'success');
  }
  function start() {
    try {
      const pending = JSON.parse(sessionStorage.getItem('app-next-notice') || 'null');
      sessionStorage.removeItem('app-next-notice');
      if (pending && pending.text) window.appNotify(pending.text, pending.kind);
    } catch (_) {}
    document.querySelectorAll(selectors).forEach(node => seen.set(node, (node.textContent || '').trim()));
    new MutationObserver(records => {
      const changed = new Set();
      for (const record of records) {
        const element = record.target.nodeType === 1 ? record.target : record.target.parentElement;
        const target = element && element.closest(selectors);
        if (target) changed.add(target);
      }
      changed.forEach(inspect);
    }).observe(document.body, {subtree:true, childList:true, characterData:true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
})();
