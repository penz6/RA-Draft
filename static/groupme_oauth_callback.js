/* GroupMe's documented implicit callback: keep tokens out of page links/history. */
(() => {
  'use strict';
  const query = new URLSearchParams(window.location.search);
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  window.history.replaceState(null, '', window.location.pathname);
  const form = document.getElementById('groupme-oauth-complete');
  if (!form) return;
  form.elements.access_token.value = fragment.get('access_token') || query.get('access_token') || '';
  form.elements.state.value = fragment.get('state') || query.get('state') || '';
  form.requestSubmit();
})();
