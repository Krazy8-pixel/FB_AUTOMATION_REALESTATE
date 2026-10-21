(() => {
  const nativeFetch = window.fetch.bind(window);
  function showProblem(message) {
    let notice = document.getElementById('app-connection-notice');
    if (!notice) {
      notice = document.createElement('div');
      notice.id = 'app-connection-notice';
      notice.setAttribute('role', 'status');
      notice.style.cssText = 'padding:12px;background:#fff3cd;color:#513c00;border:1px solid #e3cf88';
      document.querySelector('.main')?.prepend(notice);
    }
    notice.textContent = message;
  }
  window.appFetch = async (url, options = {}) => {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    const external = options.signal;
    if (external?.aborted) cancel();
    else external?.addEventListener('abort', cancel, {once:true});
    const timer = setTimeout(cancel, 15000);
    try {
      const response = await nativeFetch(url, {...options, signal:controller.signal});
      // Include a stalled response body in the deadline, not just its headers.
      await response.clone().arrayBuffer();
      if (response.status === 503) {
        showProblem('Another action is taking longer than expected. Check Posting Results before retrying a post.');
        throw new Error('The app is busy. Please try again shortly.');
      }
      return response;
    } catch (error) {
      if (controller.signal.aborted && !external?.aborted) {
        const message = 'The app took too long to respond. Posting may still complete; check Posting Results before trying again.';
        showProblem(message);
        throw new Error(message);
      }
      throw error;
    } finally {
      clearTimeout(timer);
      external?.removeEventListener('abort', cancel);
    }
  };
})();
