(() => {
  const dialog = document.getElementById('backup-edit-dialog');
  const form = document.getElementById('backup-content-form');
  if (!dialog || !form) return;
  const feedback = document.getElementById('backup-edit-feedback');
  document.querySelectorAll('[data-backup-edit-open]').forEach(button => button.addEventListener('click', () => { feedback.textContent = ''; dialog.showModal(); }));
  dialog.querySelectorAll('[data-backup-edit-close]').forEach(button => button.addEventListener('click', () => { form.reset(); dialog.close(); }));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true; feedback.textContent = '';
    try {
      const response = await fetch(form.action, {method:'POST', body:new FormData(form)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Could not save backup.');
      try { sessionStorage.setItem('app-next-notice', JSON.stringify({text:'Your backup has been saved.', kind:'success'})); } catch (_) {}
      location.reload();
    } catch (error) { feedback.textContent = error.message || 'Could not save backup.'; button.disabled = false; }
  });
})();
