(function() {
  'use strict';
  if (sessionStorage.getItem('fos_exit_shown') || localStorage.getItem('fos_purchased')) return;

  var shown = false, ready = false, pageStart = Date.now();
  setTimeout(function() { ready = true; }, 15000);
  setTimeout(function() { if (ready) show(); }, 60000);

  // Desktop: mouse leaves toward top
  document.addEventListener('mouseout', function(e) {
    if (!e.relatedTarget && e.clientY < 10 && ready) show();
  });

  // Mobile: fast scroll-up
  var lastY = 0, lastT = 0;
  window.addEventListener('scroll', function() {
    var y = window.scrollY, t = Date.now(), dy = lastY - y, dt = t - lastT;
    if (dy > 100 && dt < 300 && ready) show();
    lastY = y; lastT = t;
  }, { passive: true });

  function show() {
    if (shown) return;
    shown = true;
    sessionStorage.setItem('fos_exit_shown', '1');

    var style = document.createElement('style');
    style.textContent = [
      '@keyframes fosSlideUp{from{opacity:0;transform:translateY(40px)}to{opacity:1;transform:translateY(0)}}',
      '.fos-overlay{position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,.85);',
      'backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);display:flex;',
      'align-items:center;justify-content:center;font-family:system-ui,-apple-system,sans-serif}',
      '.fos-modal{background:#0f172a;border:1px solid rgba(168,85,247,.3);border-radius:20px;',
      'max-width:520px;width:92%;padding:40px 36px;position:relative;color:#e2e8f0;',
      'animation:fosSlideUp .3s ease}',
      '.fos-close{position:absolute;top:14px;right:18px;background:none;border:none;',
      'color:#94a3b8;font-size:28px;cursor:pointer;line-height:1;padding:4px}',
      '.fos-close:hover{color:#e2e8f0}',
      '.fos-h{font-size:26px;font-weight:700;margin:0 0 10px;',
      'background:linear-gradient(135deg,#a855f7,#ec4899);-webkit-background-clip:text;',
      '-webkit-text-fill-color:transparent;background-clip:text}',
      '.fos-sub{color:#94a3b8;font-size:15px;margin:0 0 20px;line-height:1.5}',
      '.fos-ul{list-style:none;padding:0;margin:0 0 22px}',
      '.fos-ul li{padding:6px 0;font-size:15px;color:#e2e8f0}',
      '.fos-ul li::before{content:"\\2713";color:#a855f7;font-weight:700;margin-right:10px}',
      '.fos-price{text-align:center;margin:0 0 18px;font-size:15px;color:#94a3b8}',
      '.fos-price s{margin-right:6px}',
      '.fos-price span{color:#ec4899;font-size:22px;font-weight:700}',
      '.fos-price em{font-style:normal;background:linear-gradient(135deg,#a855f7,#ec4899);',
      'color:#fff;padding:2px 8px;border-radius:6px;font-size:13px;margin-left:8px}',
      '.fos-cta{display:block;width:100%;padding:16px;border:none;border-radius:12px;',
      'background:linear-gradient(135deg,#a855f7,#ec4899);color:#fff;font-size:17px;',
      'font-weight:700;cursor:pointer;text-align:center;text-decoration:none;box-sizing:border-box}',
      '.fos-cta:hover{opacity:.9}',
      '.fos-skip{display:block;text-align:center;margin-top:14px;color:#64748b;',
      'font-size:13px;cursor:pointer;background:none;border:none;text-decoration:underline}',
      '.fos-skip:hover{color:#94a3b8}',
      '.fos-badge{text-align:center;margin-top:16px;font-size:12px;color:#64748b}'
    ].join('');
    document.head.appendChild(style);

    var overlay = document.createElement('div');
    overlay.className = 'fos-overlay';
    overlay.innerHTML =
      '<div class="fos-modal">' +
        '<button class="fos-close" aria-label="Close">&times;</button>' +
        '<h2 class="fos-h">Wait — Don\'t Leave Without This</h2>' +
        '<p class="fos-sub">You just used one of our 91 free tools. Imagine having ALL of them connected in one Notion workspace.</p>' +
        '<ul class="fos-ul">' +
          '<li>Complete freelance business system</li>' +
          '<li>Pre-built dashboards &amp; automations</li>' +
          '<li>Lifetime updates — pay once</li>' +
        '</ul>' +
        '<p class="fos-price"><s>$29</s> <span>$17.40</span> <em>40% OFF with FLASH40</em></p>' +
        '<a class="fos-cta" href="https://buy.stripe.com/eVa9Ek5OOaGF1EkcMM?prefilled_promo_code=FLASH40">Get FreelanceOS Pro — $17.40</a>' +
        '<button class="fos-skip">No thanks, I’ll keep using free tools</button>' +
        '<div class="fos-badge">\u{1f6e1}️ 30-Day Money-Back Guarantee</div>' +
      '</div>';

    document.body.appendChild(overlay);

    function close() { overlay.remove(); }

    overlay.querySelector('.fos-close').addEventListener('click', close);
    overlay.querySelector('.fos-skip').addEventListener('click', close);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) close(); });
    document.addEventListener('keydown', function handler(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', handler); }
    });
  }
})();
