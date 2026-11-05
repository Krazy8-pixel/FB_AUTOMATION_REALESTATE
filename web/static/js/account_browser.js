(() => {
  const account = document.getElementById('browser-account');
  const surface = document.getElementById('browser-surface');
  const content = document.querySelector('.content');
  const topbar = document.querySelector('.topbar');
  const menuToggle = document.getElementById('browser-menu-toggle');
  const placeholder = document.getElementById('browser-placeholder');
  const status = document.getElementById('browser-status');
  const open = document.getElementById('browser-open');
  const stop = document.getElementById('browser-stop');
  const posting = document.getElementById('browser-posting');
  const groupPosts = document.getElementById('browser-group-posts');
  const marketplaceAds = document.getElementById('browser-marketplace-ads');
  let openedAccount = null;
  let mobileView = false;
  let layoutPending = false;
  let layoutAgain = false;

  async function post(path, payload) {
    const slowAction = /\/(open|posting|desktop|stop)$/.test(path);
    const response = await appFetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), timeoutMs: slowAction ? 90000 : 15000});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || 'Browser action failed.');
    return data;
  }
  function render(state) {
    openedAccount = state.account || null;
    stop.disabled = !state.open;
    mobileView = !!state.mobile;
    surface.classList.toggle('is-mobile', mobileView);
    posting.textContent = mobileView ? 'Close posting page' : 'Open posting page';
    placeholder.hidden = !!state.open;
    status.classList.toggle('is-open', !!state.open);
    open.textContent = state.open && account.value !== state.account ? 'Switch account' : 'Open account';
    status.textContent = state.open ? state.account + ' is open' : 'Choose an account to open';
    if (state.open) layout();
  }
  async function layout() {
    if (layoutPending) { layoutAgain = true; return; }
    if (content && topbar) content.style.paddingTop = Math.ceil(topbar.getBoundingClientRect().bottom + 6) + 'px';
    if (!openedAccount || document.hidden) return;
    layoutPending = true;
    const box = surface.getBoundingClientRect();
    const rect = {left: box.left + surface.clientLeft, top: box.top + surface.clientTop,
      width: surface.clientWidth, height: surface.clientHeight};
    rect.right = rect.left + rect.width;
    rect.bottom = rect.top + rect.height;
    try {
      if (rect.top < 0 || rect.bottom > window.innerHeight || rect.left < 0 || rect.right > window.innerWidth) {
        await post('/account/browser/hide', {});
        return;
      }
      await post('/account/browser/layout', {x: rect.left, y: rect.top, width: rect.width,
        height: rect.height, viewport_width: window.innerWidth, viewport_height: window.innerHeight});
    } catch (error) { status.textContent = error.message; }
    finally {
      layoutPending = false;
      if (layoutAgain) { layoutAgain = false; requestAnimationFrame(layout); }
    }
  }
  open.addEventListener('click', async () => {
    open.disabled = true; status.textContent = 'Opening account...';
    try { render(await post('/account/browser/open', {account: account.value})); }
    catch (error) { status.textContent = error.message; }
    finally { open.disabled = false; }
  });
  posting.addEventListener('click', async () => {
    posting.disabled = true;
    status.textContent = mobileView ? 'Restoring desktop view...' : 'Opening mobile posting page...';
    try {
      const result = mobileView ? await post('/account/browser/desktop', {}) : await post('/account/browser/posting', {account: account.value});
      render(result);
    } catch (error) { status.textContent = error.message; }
    finally { posting.disabled = !account.value; }
  });
  async function openDestination(button, path, label) {
    button.disabled = true;
    status.textContent = 'Opening ' + label + '...';
    try { render(await post(path, {account: account.value})); }
    catch (error) { status.textContent = error.message; }
    finally { button.disabled = !account.value; }
  }
  groupPosts.addEventListener('click', () => openDestination(groupPosts, '/account/browser/group-posts', 'group posts'));
  marketplaceAds.addEventListener('click', () => openDestination(marketplaceAds, '/account/browser/marketplace-ads', 'Marketplace ads'));
  stop.addEventListener('click', async () => {
    stop.disabled = true;
    status.textContent = 'Returning to Facebook home...';
    try { render(await post('/account/browser/stop', {})); }
    catch (error) { status.textContent = error.message; stop.disabled = false; }
  });
  account.addEventListener('change', () => { open.textContent = openedAccount && openedAccount !== account.value ? 'Switch account' : 'Open account'; });
  if (menuToggle) menuToggle.addEventListener('click', async () => {
    if (openedAccount) {
      try { await post('/account/browser/hide', {}); } catch (error) { status.textContent = error.message; }
    }
    const expanded = document.body.classList.toggle('account-browser-sidebar-open');
    menuToggle.setAttribute('aria-expanded', String(expanded));
    menuToggle.setAttribute('aria-label', expanded ? 'Hide main menu' : 'Show main menu');
    menuToggle.title = expanded ? 'Hide main menu' : 'Show main menu';
    requestAnimationFrame(layout);
  });
  window.addEventListener('resize', layout);
  document.addEventListener('scroll', layout, true);
  // The embedded Facebook account browser belongs to this page only. When
  // the user navigates to another section (or closes this document), stop it
  // rather than merely hiding it so the Facebook session/window is released.
  window.addEventListener('pagehide', () => {
    navigator.sendBeacon('/account/browser/leave', new Blob(['{}'], {type: 'application/json'}));
  });
  fetch('/account/browser/status').then(response => response.json()).then(render).catch(() => {});
  layout();
})();
