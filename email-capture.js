/**
 * FreelanceOS Email Capture
 * Usage: include via <script src="/email-capture.js"></script>
 * Optionally set window.BEEHIIV_PUB_ID and window.BEEHIIV_API_KEY before including.
 * Exposes: window.captureEmail(email, source) → Promise<{success, method}>
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'freelanceos_leads';

  function saveToLocalStorage(email, source) {
    try {
      var existing = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      existing.push({ email: email, source: source || 'freelanceos-storefront', timestamp: new Date().toISOString() });
      localStorage.setItem(STORAGE_KEY, JSON.stringify(existing));
    } catch (e) {
      // silently ignore storage errors
    }
  }

  window.captureEmail = function (email, source) {
    var resolvedSource = source || 'freelanceos-storefront';

    // Always persist locally
    saveToLocalStorage(email, resolvedSource);

    // If Beehiiv credentials are configured, also send there
    if (
      typeof window.BEEHIIV_PUB_ID === 'string' && window.BEEHIIV_PUB_ID &&
      typeof window.BEEHIIV_API_KEY === 'string' && window.BEEHIIV_API_KEY
    ) {
      return fetch(
        'https://api.beehiiv.com/v2/publications/' + window.BEEHIIV_PUB_ID + '/subscriptions',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + window.BEEHIIV_API_KEY
          },
          body: JSON.stringify({
            email: email,
            utm_source: resolvedSource,
            reactivate_existing: true,
            send_welcome_email: true
          })
        }
      ).then(function (res) {
        if (!res.ok) throw new Error('Beehiiv responded with ' + res.status);
        return { success: true, method: 'beehiiv' };
      }).catch(function () {
        // Beehiiv failed — already saved locally, resolve gracefully
        return { success: true, method: 'localstorage' };
      });
    }

    // No Beehiiv credentials — local only
    return Promise.resolve({ success: true, method: 'localstorage' });
  };
})();
