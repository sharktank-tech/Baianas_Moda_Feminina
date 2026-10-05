/* ── Navegação entre painéis ──────────────────────────── */
const PANEL_TITLES = {
  overview:     'Dashboard',
  products:     'Produtos',
  promotions:   'Promoções',
  suppliers:    'Fornecedores',
  users:        'Usuários',
  testimonials: 'Depoimentos',
  hero:         'Hero slides',
  siteconfig:   'Configurações do site',
  footer:       'Rodapé & Links',
};

const PANEL_ACTIONS = {
  products:     `<a href="/admin/products/add"    class="adm-btn adm-btn-primary adm-btn-sm">+ Produto</a>`,
  promotions:   `<a href="/admin/add_promotion"   class="adm-btn adm-btn-primary adm-btn-sm">+ Promoção</a>`,
  suppliers:    `<a href="/admin/add_supplier"    class="adm-btn adm-btn-primary adm-btn-sm">+ Fornecedor</a>`,
  users:        `<a href="/admin/users/add"       class="adm-btn adm-btn-dark adm-btn-sm">+ Usuário</a>`,
  testimonials: `<a href="/admin/add_testimonial" class="adm-btn adm-btn-primary adm-btn-sm">+ Depoimento</a>`,
  hero:         `<a href="/admin/add_hero_slide"  class="adm-btn adm-btn-primary adm-btn-sm">+ Slide</a>`,
};

function activatePanel(id) {
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

  /* URL hash para manter estado ao recarregar */
  history.replaceState(null, '', '#' + id);
}

document.querySelectorAll('[data-panel]').forEach(btn => {
  btn.addEventListener('click', () => activatePanel(btn.dataset.panel));
});

/* Restaura painel do hash */
const hash = location.hash.replace('#', '');
if (hash && document.getElementById('panel-' + hash)) {
  activatePanel(hash);
} else {
  activatePanel('overview');
}

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