(() => {
  const page = document.querySelector('.listing-detail');
  if (!page) return;
  const readView = document.getElementById('listing-read-view');
  const editForm = document.getElementById('listing-edit-form');
  const feedback = document.getElementById('workflow-status');
  const crossDialog = document.getElementById('property-cross-dialog');
  const crossMessage = document.getElementById('property-cross-message');
  const crossButton = document.getElementById('start-cross-post');
  const deleteButton = document.getElementById('start-delete');
  const deleteOptions = document.getElementById('delete-options');
  let deleting = false;

  function message(text, error = false) {
    feedback.textContent = text || '';
    feedback.classList.toggle('is-error', error);
  }
  function closeDeleteOptions() {
    deleteOptions.hidden = true;
    deleteButton.setAttribute('aria-expanded', 'false');
  }

  document.getElementById('edit-listing').addEventListener('click', () => {
    readView.hidden = true;
    editForm.hidden = false;
    editForm.scrollIntoView({block: 'start', behavior: 'smooth'});
  });
  document.getElementById('cancel-edit-listing').addEventListener('click', () => {
    editForm.reset();
    editForm.hidden = true;
    readView.hidden = false;
    message('');
  });
  editForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = editForm.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      const response = await appFetch(page.dataset.saveUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'save_details', fields: Object.fromEntries(new FormData(editForm))})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Could not save property details.');
      window.location.reload();
    } catch (error) {
      message(error.message || 'Could not save property details.', true);
      button.disabled = false;
    }
  });

  document.querySelectorAll('[data-preview]').forEach(button => button.addEventListener('click', () => {
    document.getElementById('listing-hero').src = button.dataset.preview;
    document.querySelectorAll('[data-preview]').forEach(item => item.classList.toggle('is-active', item === button));
  }));

  if (crossButton && crossDialog) crossButton.addEventListener('click', () => {
    crossMessage.textContent = '';
    crossDialog.showModal();
  });
  document.querySelector('[data-cross-cancel]').addEventListener('click', () => crossDialog.close());
  document.getElementById('property-cross-submit').addEventListener('click', async event => {
    const button = event.currentTarget;
    const targets = [...crossDialog.querySelectorAll('input[type="checkbox"]:checked')].map(input => input.value);
    if (!targets.length || targets.length >= 20) {
      crossMessage.textContent = 'Select 1 to 19 eligible accounts.';
      return;
    }
    button.disabled = true;
    crossMessage.textContent = 'Queueing property ad...';
    try {
      const response = await appFetch(page.dataset.crossUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          account: page.dataset.account,
          listing_id: page.dataset.listingId,
          target_accounts: targets,
          fields: JSON.parse(page.dataset.propertyFields || '{}'),
          image_order: JSON.parse(page.dataset.imageOrder || '[]'),
          post_mode: 'now',
          backup_post: crossButton.dataset.propertyBackup === 'true',
          posting_requested_at_ms: Date.now()
        })
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Could not queue property ad.');
      crossDialog.close();
      message(result.message || 'Property ad queued. Check Posting Results.');
    } catch (error) {
      crossMessage.textContent = error.message || 'Could not reach the app. Try again.';
    } finally {
      button.disabled = false;
    }
  });

  deleteButton.addEventListener('click', () => {
    if (deleting) return;
    deleteOptions.hidden = !deleteOptions.hidden;
    deleteButton.setAttribute('aria-expanded', String(!deleteOptions.hidden));
  });
  document.getElementById('cancel-delete').addEventListener('click', closeDeleteOptions);
  deleteOptions.addEventListener('click', async event => {
    const choice = event.target.closest('[data-delete-target]');
    if (!choice || deleting) return;
    deleting = true;
    closeDeleteOptions();
    deleteButton.disabled = true;
    message('Starting deletion...');
    try {
      const response = await appFetch(page.dataset.deleteUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: [page.dataset.listingId], target: choice.dataset.deleteTarget, account: page.dataset.account})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Deletion could not start.');
      if (result.completed) { window.location.assign(page.dataset.listingsUrl); return; }
      message('Deletion started. This page will update when it finishes.');
    } catch (error) {
      message(error.message || 'Could not start deletion.', true);
      deleteButton.disabled = false;
      deleting = false;
    }
  });
})();
