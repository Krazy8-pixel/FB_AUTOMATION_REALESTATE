(() => {
  const nativeFetch = window.fetch.bind(window);
  function showProblem(message) {
    if (window.appNotify) window.appNotify(message, 'failed');
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
      // Show messages returned by user-initiated actions in the same box on
      // every page. Background layout and status requests have no popup.
      const method = String(options.method || 'GET').toUpperCase();
      const path = String(url);
      if (method !== 'GET' && !/\/account\/browser\/(layout|hide)(?:\?|$)/.test(path)
          && (response.headers.get('content-type') || '').includes('application/json')) {
        try {
          const result = await response.clone().json();
          if (typeof result.message === 'string' && result.message.trim() && window.appNotify) {
            window.appNotify(result.message, !response.ok || result.ok === false ? 'failed' : 'success');
          }
        } catch (_) {}
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
