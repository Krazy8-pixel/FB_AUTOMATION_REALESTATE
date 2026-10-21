(() => {
  const form = document.querySelector('.header-mode-form');
  if (!form) return;
  const toggle = form.querySelector('[name="run_in_background"]');
  const label = form.querySelector('.background-toggle-state');
  const message = form.querySelector('.header-mode-message');
  let saved = toggle.checked;
  async function save(event) {
    event.preventDefault();
    if (toggle.disabled) return;
    const body = new FormData(form);
    toggle.disabled = true;
    message.hidden = true;
    try {
      const response = await appFetch(form.action, {
        method: 'POST', body, headers: {'X-Requested-With': 'XMLHttpRequest'},
      });
      if (!response.ok || response.redirected) throw new Error();
      const data = await response.json();
      if (!data.ok) throw new Error();
      saved = data.enabled;
      toggle.checked = saved;
      label.textContent = saved ? 'Background' : 'Visible browser';
      form.title = 'Saved. This mode applies the next time an automation browser opens.';
    } catch (_) {
      toggle.checked = saved;
      message.textContent = 'Could not save browser mode. Please try again.';
      message.hidden = false;
    } finally {
      toggle.disabled = false;
    }
  }
  toggle.addEventListener('change', save);
  form.addEventListener('submit', save);
})();
