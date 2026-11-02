/* Capture the final Post action before uploads, queueing, or navigation. */
(() => {
  const clicks = new WeakMap();
  function start(form) {
    const timestamp = Date.now();
    let input = form.querySelector('input[name="posting_requested_at_ms"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'posting_requested_at_ms';
      form.appendChild(input);
    }
    input.disabled = false;
    input.value = String(timestamp);
    clicks.set(form, timestamp);
    return timestamp;
  }
  function forSubmit(form) {
    const timestamp = clicks.get(form);
    return timestamp !== undefined && Date.now() - timestamp < 1000
      ? timestamp : start(form);
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('button, input[type="submit"], input[type="image"]');
    if (!button || button.disabled) return;
    const external = button.dataset.postingTimingForm;
    const form = external ? document.getElementById(external) : button.form;
    if (form?.hasAttribute('data-posting-timing') &&
        (external || button.type === 'submit' || button.type === 'image')) start(form);
  }, true);
  document.addEventListener('submit', event => {
    if (event.target.hasAttribute('data-posting-timing')) forSubmit(event.target);
  }, true);
  document.addEventListener('invalid', event => {
    if (event.target.form) clicks.delete(event.target.form);
  }, true);
  window.postingTiming = {start, forSubmit};
})();
