/* ── Navegação entre painéis ────────────────────────────
   Abas com estado no fragmento da URL (#painel ou #painel?params):
   - clique usa pushState: voltar/avançar do navegador funcionam;
   - recarregar ou abrir direto ativa a aba do fragmento;
   - painéis de operação carregam sob demanda (fetch da parcial).
*/
const PANEL_TITLES = {
  overview:           'Dashboard',
  products:           'Produtos',
  promotions:         'Promoções',
  coupons:            'Cupons',
  suppliers:          'Fornecedores',
  users:              'Usuários',
  testimonials:       'Depoimentos',
  hero:               'Hero slides',
  siteconfig:         'Configurações do site',
  footer:             'Rodapé & Links',
  orders:             'Pedidos',
  'manual-review':    'Revisão manual',
  'sales-readiness':  'Prontidão para vendas',
  'checkout-recovery':'Recuperação de checkout',
};

const PANEL_ACTIONS = {
  products:     `<a href="/admin/products/add"    class="adm-btn adm-btn-primary adm-btn-sm">+ Produto</a>`,
  promotions:   `<a href="/admin/add_promotion"   class="adm-btn adm-btn-primary adm-btn-sm">+ Promoção</a>`,
  suppliers:    `<a href="/admin/add_supplier"    class="adm-btn adm-btn-primary adm-btn-sm">+ Fornecedor</a>`,
  users:        `<a href="/admin/users/add"       class="adm-btn adm-btn-dark adm-btn-sm">+ Usuário</a>`,
  testimonials: `<a href="/admin/add_testimonial" class="adm-btn adm-btn-primary adm-btn-sm">+ Depoimento</a>`,
  hero:         `<a href="/admin/add_hero_slide"  class="adm-btn adm-btn-primary adm-btn-sm">+ Slide</a>`,
};

/* Painéis com conteúdo carregado sob demanda (container vazio no HTML). */
const LAZY_PANELS = new Set([
  'orders', 'manual-review', 'sales-readiness', 'checkout-recovery',
]);

/* Buscas livres nunca viajam no fragmento (podem conter dados pessoais);
   estado e página, sim. */
const HASH_PARAMS_ALLOW = new Set(['status', 'page']);

function parseHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) return { panel: 'overview', query: '' };
  const cut = raw.indexOf('?');
  const panel = cut === -1 ? raw : raw.slice(0, cut);
  const query = cut === -1 ? '' : raw.slice(cut + 1);
  if (!document.getElementById('panel-' + panel)) {
    return { panel: 'overview', query: '' };
  }
  return { panel, query };
}

function hashFor(panel, query) {
  return '#' + panel + (query ? '?' + query : '');
}

/* fetch em voo por container (evita duplicar a mesma carga). */
const inflight = new Map();

function containerOf(panel) {
  return document.querySelector('[data-partial-container="' + panel + '"]');
}

function showPartialError(container, url) {
  container.innerHTML =
    '<div class="adm-card"><p class="adm-empty">' +
    'Falha ao carregar este painel. ' +
    '<button type="button" class="adm-btn adm-btn-outline adm-btn-sm" ' +
    'data-partial-retry> Tentar novamente</button></p></div>';
  container.dataset.partialLoaded = '';
  container.dataset.partialUrl = url;
}

function fetchInto(container, url) {
  container.dataset.partialLoaded = '';
  container.innerHTML = '<p class="adm-empty">Carregando…</p>';
  return fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
    .then(resp => {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.text();
    })
    .then(html => {
      container.innerHTML = html;
      container.dataset.partialLoaded = url;
    })
    .catch(() => showPartialError(container, url));
}

function loadPartial(panel, query) {
  const container = containerOf(panel);
  if (!container) return Promise.resolve();
  let url = container.dataset.partialSrc;
  if (query) url += (url.indexOf('?') === -1 ? '?' : '&') + query;
  if (container.dataset.partialLoaded === url || inflight.has(url)) {
    return inflight.get(url) || Promise.resolve();
  }
  const done = fetchInto(container, url).finally(() => inflight.delete(url));
  inflight.set(url, done);
  return done;
}

function activatePanel(id, opts) {
  const options = opts || {};
  /* painéis */
  document.querySelectorAll('.adm-panel').forEach(p => p.classList.remove('active'));
  const target = document.getElementById('panel-' + id);
  if (target) target.classList.add('active');

  /* links nav (sidebar + mobile) */
  document.querySelectorAll('[data-panel]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.panel === id);
  });

  /* topbar */
  document.getElementById('adm-current-title').textContent = PANEL_TITLES[id] || id;
  document.getElementById('adm-topbar-actions').innerHTML  =
    PANEL_ACTIONS[id] ||
    '<a href="/" class="adm-btn adm-btn-outline adm-btn-sm">Ver loja</a>';

  /* conteúdo sob demanda + URL */
  const query = options.query || '';
  if (LAZY_PANELS.has(id) && options.load !== false) {
    loadPartial(id, query);
  }
  const next = hashFor(id, query);
  if (location.hash !== next) {
    if (options.push === false) {
      history.replaceState(null, '', next);
    } else {
      history.pushState({ admPanel: id }, '', next);
    }
  }
}

function activateFromHash(push) {
  const parsed = parseHash();
  activatePanel(parsed.panel, { push: push, query: parsed.query });
}

document.querySelectorAll('[data-panel]').forEach(btn => {
  btn.addEventListener('click', () => activatePanel(btn.dataset.panel));
});

/* Voltar/avançar e edição manual do fragmento. */
window.addEventListener('popstate', () => activateFromHash(false));
window.addEventListener('hashchange', () => activateFromHash(false));

/* Navegação interna das parciais (filtros/paginação) sem sair do dashboard. */
document.addEventListener('click', e => {
  const retry = e.target.closest('[data-partial-retry]');
  if (retry) {
    const container = retry.closest('[data-partial-container]');
    if (container) {
      container.dataset.partialLoaded = '';
      loadPartial(container.dataset.partialContainer, '');
    }
    return;
  }
  const link = e.target.closest('a[data-partial-link]');
  if (!link) return;
  e.preventDefault();
  const container = link.closest('[data-partial-container]');
  if (!container) return;
  /* Fetch usa a URL completa do link (inclusive busca livre q); o hash
     guarda só parâmetros não pessoais. */
  const url = new URL(link.getAttribute('href'), location.origin);
  const panel = container.dataset.partialContainer;
  const kept = new URLSearchParams();
  url.searchParams.forEach((value, key) => {
    if (HASH_PARAMS_ALLOW.has(key) && value) kept.set(key, value);
  });
  fetchInto(container, url.toString());
  activatePanel(panel, { query: kept.toString(), load: false });
});

/* Formulários GET das parciais (filtros) sem sair do dashboard. */
document.addEventListener('submit', e => {
  const form = e.target.closest ? e.target.closest('form[data-partial-form]') : null;
  if (!form || (form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
  e.preventDefault();
  const container = form.closest('[data-partial-container]');
  if (!container) return;
  const panel = container.dataset.partialContainer;
  const data = new URLSearchParams(new FormData(form));
  data.set('page', '1');
  const kept = new URLSearchParams();
  data.forEach((value, key) => {
    if (HASH_PARAMS_ALLOW.has(key) && value) kept.set(key, value);
  });
  /* Busca livre (q) aplica no fetch sem viajar no fragmento. */
  let url = container.dataset.partialSrc;
  url += (url.indexOf('?') === -1 ? '?' : '&') + data.toString();
  fetchInto(container, url);
  activatePanel(panel, { query: kept.toString(), load: false });
});

/* Restaura painel do fragmento (abertura direta ou recarregar). */
activateFromHash(false);

/* ── Busca rápida de produtos ─────────────────────────── */
const searchInput = document.getElementById('searchProducts');
if (searchInput) {
  searchInput.addEventListener('input', function () {
    const q = this.value.toLowerCase();
    document.querySelectorAll('#tableProducts tbody tr').forEach(tr => {
      tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
}
