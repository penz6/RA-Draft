/* Navigate after the local form has completed, outside its CSP form-action chain. */
(() => {
  'use strict';
  const link = document.getElementById('groupme-authorize-link');
  if (link) window.location.replace(link.href);
})();
