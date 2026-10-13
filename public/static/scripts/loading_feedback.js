/* ── Feedback de carregamento (site + admin) ─────────────
   Somente apresentação: nunca impede, repete ou altera navegações e
   envios. Sem fetch/retry aqui (GETs sob demanda vivem no dashboard).
   - Barra fina de progresso em navegação interna (indeterminada, com
     atraso anti-flash). Não toca na navegação em si.
   - Botão acionado: spinner + texto contextual + aria-busy, com largura
     preservada; 2º envio bloqueado sem perder name/value do 1º.
   - Formulários com mecanismo próprio (`#checkoutForm`, `data-once`) ou
     com `data-loading="off"` são ignorados por este handler.
   - `pageshow` restaura tudo (voltar do navegador / bfcache).
   - Sem JS, nada muda: barra e spinner só existem via DOM.
*/
(function () {
  'use strict';

  var PROGRESS_DELAY_MS = 150;
  var DEFAULT_TEXT = 'Processando…';

  function isModifiedClick(event) {
    return event.ctrlKey || event.metaKey || event.shiftKey || event.altKey
      || (event.button !== undefined && event.button !== 0);
  }

  function isSkippableLink(anchor) {
    if (!anchor || !anchor.href) return true;
    var href = anchor.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#') return true;
    var lower = href.toLowerCase();
    if (lower.indexOf('mailto:') === 0 || lower.indexOf('tel:') === 0) {
      return true;
    }
    if (anchor.hasAttribute('download')) return true;
    if (anchor.target && anchor.target !== '_self') return true;
    if (anchor.getAttribute('data-loading') === 'off') return true;
    try {
      var url = new URL(anchor.href, location.href);
      if (url.origin !== location.origin) return true;
    } catch (err) {
      return true;
    }
    return false;
  }

  var progressTimer = null;

  function showProgress() {
    var bar = document.getElementById('page-progress');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'page-progress';
      bar.setAttribute('role', 'status');
      bar.setAttribute('aria-label', 'Carregando página');
      bar.setAttribute('aria-hidden', 'true');
      document.body.appendChild(bar);
    }
    bar.classList.add('is-active');
  }

  document.addEventListener('click', function (event) {
    if (isModifiedClick(event)) return;
    var anchor = event.target && event.target.closest
      ? event.target.closest('a[href]')
      : null;
    if (!anchor || isSkippableLink(anchor)) return;
    if (progressTimer) return;
    progressTimer = setTimeout(showProgress, PROGRESS_DELAY_MS);
  });

  function buttonLabel(button) {
    return button.getAttribute('data-loading-text') || DEFAULT_TEXT;
  }

  function armButton(button) {
    if (!button || button.dataset.loadingActive) return;
    button.dataset.loadingActive = '1';
    button.dataset.loadingOriginal = button.innerHTML;
    if (!button.style.minWidth) {
      button.style.minWidth = button.offsetWidth + 'px';
    }
    var spinner = document.createElement('span');
    spinner.className = 'loading-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    button.setAttribute('aria-busy', 'true');
    button.textContent = buttonLabel(button);
    button.insertBefore(spinner, button.firstChild);
  }

  function disarmButton(button) {
    if (!button || !button.dataset.loadingActive) return;
    delete button.dataset.loadingActive;
    if (button.dataset.loadingOriginal !== undefined) {
      button.innerHTML = button.dataset.loadingOriginal;
      delete button.dataset.loadingOriginal;
    }
    button.style.minWidth = '';
    button.removeAttribute('aria-busy');
  }

  function findSubmitter(form, event) {
    if (event && event.submitter && event.submitter instanceof HTMLElement) {
      return event.submitter;
    }
    var active = document.activeElement;
    if (active && active instanceof HTMLElement
        && active.type === 'submit' && form.contains(active)) {
      return active;
    }
    return form.querySelector('button[type="submit"], input[type="submit"]');
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || form.getAttribute('data-loading') === 'off') return;
    if (form.hasAttribute('data-once') || form.id === 'checkoutForm') return;
    if (form.dataset.submitted === '1') {
      /* Duplo envio: bloqueia sem tocar no 1º (name/value preservados). */
      event.preventDefault();
      return;
    }
    /* checkValidity() funciona mesmo com novalidate: só perde o bloqueio
       visual nativo, não a avaliação. Inválido => sem loading. */
    if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
      return;
    }
    if (event.defaultPrevented) return;
    form.dataset.submitted = '1';
    form.setAttribute('aria-busy', 'true');
    armButton(findSubmitter(form, event));
  });

  function restoreAll() {
    if (progressTimer) {
      clearTimeout(progressTimer);
      progressTimer = null;
    }
    var bar = document.getElementById('page-progress');
    if (bar) bar.classList.remove('is-active');
    document.querySelectorAll('[data-loading-active]').forEach(disarmButton);
    document.querySelectorAll('form[data-submitted]').forEach(function (form) {
      delete form.dataset.submitted;
      form.removeAttribute('aria-busy');
    });
  }

  /* Voltar do navegador / bfcache: nunca deixar a interface travada. */
  window.addEventListener('pageshow', restoreAll);
})();
