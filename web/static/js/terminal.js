(() => {
  const output = document.getElementById('terminal-output');
  const status = document.getElementById('terminal-status');
  const follow = document.getElementById('terminal-follow');
  const pause = document.getElementById('terminal-pause');
  const copySelected = document.getElementById('terminal-copy-selected');
  let cursor = 0, paused = false, stopped = false;
  let selecting = false;
  function selectedText() {
    const selection = window.getSelection();
    return selection && output.contains(selection.anchorNode) && output.contains(selection.focusNode)
      ? selection.toString() : '';
  }
  output.addEventListener('pointerdown', () => { selecting = true; });
  window.addEventListener('pointerup', () => { selecting = false; });
  window.addEventListener('pointercancel', () => { selecting = false; });
  window.addEventListener('blur', () => { selecting = false; });
  document.addEventListener('selectionchange', () => {
    copySelected.disabled = !selectedText();
  });
  copySelected.addEventListener('mousedown', event => event.preventDefault());
  copySelected.addEventListener('click', async () => {
    const text = selectedText();
    if (!text) return;
    try { await navigator.clipboard.writeText(text); status.textContent = 'Selected text copied'; }
    catch (_) { status.textContent = 'Press Ctrl+C to copy the selected text.'; }
  });
  pause.addEventListener('click', () => {
    paused = !paused;
    pause.textContent = paused ? 'Resume' : 'Pause';
    status.textContent = paused ? 'Paused' : 'Connecting…';
  });
  document.getElementById('terminal-clear').addEventListener('click', () => { output.textContent = ''; });
  document.getElementById('terminal-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(output.textContent); status.textContent = 'Copied'; }
    catch (_) { status.textContent = 'Copy unavailable. Select the logs to copy them.'; }
  });
  document.getElementById('terminal-download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([output.textContent], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'terminal-logs.txt'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  async function poll() {
    if (stopped) return;
    if (!paused && !selecting && !selectedText()) {
      try {
        const response = await appFetch(`${output.dataset.url}?after=${cursor}`, {cache: 'no-store'});
        if (!response.ok || response.redirected) throw new Error('unavailable');
        const data = await response.json();
        if (!paused && !selecting && !selectedText()) {
          output.textContent = ((data.reset ? '' : output.textContent) + data.text).slice(-2000000);
          cursor = data.cursor;
          status.textContent = 'Live · refreshes every second';
          if (follow.checked) output.scrollTop = output.scrollHeight;
        }
      } catch (_) { status.textContent = 'Disconnected · retrying…'; }
    }
    setTimeout(poll, 1000);
  }
  window.addEventListener('pagehide', () => { stopped = true; });
  window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; poll(); } });
  poll();
})();
