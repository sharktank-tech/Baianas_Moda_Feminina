/* ── Badge de quantidade da sacola ─────────────────────────────
   Somente apresentação: nunca altera preço, estoque, checkout ou regras.
   O número vem do servidor (mesma fonte do badge inicial); aqui só
   repintamos após voltar pelo histórico, quando o navegador pode ter
   restaurado a página do bfcache com contagem antiga. Sem escrita via
   CSSOM (CSP `style-src-attr 'none'`) e sem fetch com retry.
*/
(function () {
  'use strict';

  function paint(count, display, label) {
    var link = document.querySelector('.navbar-cart');
    if (!link) return;
    var badge = link.querySelector('.cart-badge');
    if (!display) {
      if (badge && badge.parentNode) {
        badge.parentNode.removeChild(badge);
      }
    } else if (badge) {
      if (badge.textContent !== display) badge.textContent = display;
    } else {
      var wrap = link.querySelector('.cart-icon-wrap') || link;
      badge = document.createElement('span');
      badge.className = 'cart-badge';
      badge.setAttribute('aria-hidden', 'true');
      badge.textContent = display;
      wrap.appendChild(badge);
    }
    if (label) link.setAttribute('aria-label', label);
  }

  function refresh() {
    fetch('/cart/count', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) paint(data.count, data.display, data.label);
      })
      .catch(function () { /* mantém o badge atual em caso de erro */ });
  }

  window.addEventListener('pageshow', function (event) {
    if (event.persisted) refresh();
  });

  /* Gancho para fluxos futuros na mesma página (hoje tudo é full load). */
  window.updateCartBadge = paint;
})();
