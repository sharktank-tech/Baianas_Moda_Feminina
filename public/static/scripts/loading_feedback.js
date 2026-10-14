/* ── Feedback de carregamento (site + admin) ─────────────
   Somente apresentação: nunca impede, repete ou altera navegações e
   envios. Sem fetch/retry aqui (GETs sob demanda vivem no dashboard, que
   tem feedback próprio — este módulo ignora esse conteúdo).
   - Barra fina de progresso em navegação interna (indeterminada, com
     atraso anti-flash). Não toca na navegação em si; respeita
     defaultPrevented (decisão após handlers que podem cancelar).
   - Botão acionado: spinner + texto contextual + aria-busy, com largura
     e atributos anteriores restaurados; 2º envio bloqueado sem perder
     name/value do 1º (nunca desabilita antes de partir).
   - Formulários com mecanismo próprio (`#checkoutForm`, `data-once`),
     com `data-loading="off"`, `method="dialog"` ou destino em nova
     aba/janela (inclui `formtarget` do submitter) são ignorados.
   - Validação nativa/`novalidate`/`formnovalidate` respeitadas: loading
     só quando o envio não foi cancelado; marcação adiada para enxergar
     cancelamento de handler posterior.
   - `pageshow` (e cancelamentos) restaura tudo: sem interface travada.
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

  function sameDocument(url) {
    try {
      var dest = new URL(url, location.href);
      var here = new URL(location.href);
      return dest.origin === here.origin
        && dest.pathname === here.pathname
        && dest.search === here.search;
    } catch (err) {
      return false;
    }
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
    /* Conteúdo sob demanda do dashboard: feedback do próprio módulo. */
    if (anchor.closest('[data-partial-container]')) return true;
    try {
      var url = new URL(anchor.href, location.href);
      if (url.origin !== location.origin) return true;
    } catch (err) {
      return true;
    }
    /* Mesma página (só fragmento muda, em qualquer forma): sem loading. */
    if (sameDocument(anchor.href)) return true;
    return false;
  }

  var progressTimer = null;
  var progressEvent = null;

  function ensureBar() {
    var bar = document.getElementById('page-progress');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'page-progress';
      bar.setAttribute('role', 'status');
      bar.setAttribute('aria-label', 'Carregando página');
      document.body.appendChild(bar);
    }
    return bar;
  }

  function showProgress() {
    progressTimer = null;
    if (progressEvent && progressEvent.defaultPrevented) {
      progressEvent = null;
      return;
    }
    progressEvent = null;
    ensureBar().classList.add('is-active');
  }

  function hideProgress() {
    if (progressTimer) {
      clearTimeout(progressTimer);
      progressTimer = null;
    }
    progressEvent = null;
    var bar = document.getElementById('page-progress');
    if (bar) bar.classList.remove('is-active');
  }

  function armBar(event) {
    if (progressTimer) return;
    progressEvent = event || null;
    progressTimer = setTimeout(showProgress, PROGRESS_DELAY_MS);
  }

  document.addEventListener('click', function (event) {
    if (isModifiedClick(event)) return;
    if (event.defaultPrevented) return;
    var anchor = event.target && event.target.closest
      ? event.target.closest('a[href]')
      : null;
    if (!anchor || isSkippableLink(anchor)) return;
    armBar(event);
  });

  /* Navegação cancelada após o clique: sem barra presa. */
  window.addEventListener('popstate', hideProgress);
  window.addEventListener('hashchange', hideProgress);
  document.addEventListener('submit', function () { hideProgress(); });

  function buttonLabel(button) {
    return button.getAttribute('data-loading-text') || DEFAULT_TEXT;
  }

  function isInputSubmit(control) {
    return !!control && control.tagName === 'INPUT'
      && (control.getAttribute('type') || '').toLowerCase() === 'submit';
  }

  function armControl(control) {
    if (!control || control.dataset.loadingActive) return;
    control.dataset.loadingActive = '1';
    /* Guarda tudo que será modificado para restaurar depois. */
    if (control.style.minWidth) {
      control.dataset.loadingMinWidth = control.style.minWidth;
    }
    control.style.minWidth = control.offsetWidth + 'px';
    if (control.hasAttribute('aria-busy')) {
      control.dataset.loadingAriaBusy = control.getAttribute('aria-busy');
    }
    control.setAttribute('aria-busy', 'true');
    if (isInputSubmit(control)) {
      /* <input>: nunca sobrescrever `value` (é o dado enviado).
         Indicador adjacente + texto via aria. */
      var spinner = document.createElement('span');
      spinner.className = 'loading-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      control.parentNode.insertBefore(spinner, control);
      control.dataset.loadingSpinner = '1';
    } else {
      control.dataset.loadingOriginal = control.innerHTML;
      var spin = document.createElement('span');
      spin.className = 'loading-spinner';
      spin.setAttribute('aria-hidden', 'true');
      control.textContent = buttonLabel(control);
      control.insertBefore(spin, control.firstChild);
    }
  }

  function disarmControl(control) {
    if (!control || !control.dataset.loadingActive) return;
    delete control.dataset.loadingActive;
    if (control.dataset.loadingSpinner) {
      var prev = control.previousSibling;
      if (prev && prev.className === 'loading-spinner') {
        prev.parentNode.removeChild(prev);
      }
      delete control.dataset.loadingSpinner;
    }
    if (control.dataset.loadingOriginal !== undefined) {
      control.innerHTML = control.dataset.loadingOriginal;
      delete control.dataset.loadingOriginal;
    }
    if (control.dataset.loadingMinWidth !== undefined) {
      control.style.minWidth = control.dataset.loadingMinWidth;
      delete control.dataset.loadingMinWidth;
    } else {
      control.style.minWidth = '';
    }
    if (control.dataset.loadingAriaBusy !== undefined) {
      control.setAttribute('aria-busy', control.dataset.loadingAriaBusy);
      delete control.dataset.loadingAriaBusy;
    } else {
      control.removeAttribute('aria-busy');
    }
  }

  function opensElsewhere(form, submitter) {
    if ((form.getAttribute('method') || 'get').toLowerCase() === 'dialog') {
      return true;
    }
    var target = (form.getAttribute('target') || '').toLowerCase();
    if (submitter && submitter.getAttribute
        && submitter.hasAttribute('formtarget')) {
      target = (submitter.getAttribute('formtarget') || '').toLowerCase();
    }
    return !!target && target !== '_self';
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
    if (form.closest('[data-partial-container]')) return;
    if (opensElsewhere(form, event.submitter)) return;
    if (form.dataset.submitted === '1') {
      /* Duplo envio: bloqueia sem tocar no 1º (name/value preservados). */
      event.preventDefault();
      return;
    }
    var submitter = findSubmitter(form, event);
    var skipsValidation = form.hasAttribute('novalidate')
      || !!(submitter && submitter.hasAttribute('formnovalidate'));
    if (!skipsValidation && typeof form.checkValidity === 'function'
        && !form.checkValidity()) {
      return;
    }
    if (event.defaultPrevented) return;
    /* Flag síncrona bloqueia duplicatas com determinismo; o visual é
       adiado para enxergar cancelamento de handler posterior — nesse
       caso a flag é limpa e nada fica marcado. */
    form.dataset.submitted = '1';
    var control = submitter;
    setTimeout(function () {
      if (event.defaultPrevented) {
        delete form.dataset.submitted;
        return;
      }
      if (!control || !control.isConnected) {
        delete form.dataset.submitted;
        return;
      }
      form.setAttribute('aria-busy', 'true');
      armControl(control);
    }, 0);
  });

  function restoreAll() {
    hideProgress();
    document.querySelectorAll('[data-loading-active]').forEach(disarmControl);
    document.querySelectorAll('form[data-submitted]').forEach(function (form) {
      delete form.dataset.submitted;
      form.removeAttribute('aria-busy');
    });
  }

  /* Voltar do navegador / bfcache: nunca deixar a interface travada. */
  window.addEventListener('pageshow', restoreAll);

  /* Marcador de carga (observabilidade de testes; sem efeito funcional). */
  window.__loadingFeedback = 1;
})();
