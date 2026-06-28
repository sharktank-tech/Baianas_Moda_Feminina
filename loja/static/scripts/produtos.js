/**
 * produtos.js — Pollo Modas
 *  • Busca em tempo real (filtra cards pelo nome)
 *  • Mini-slider de imagens no hover do card
 *  • Drawer de filtros mobile (open/close/overlay click)
 *  • Favoritos (toggle visual; persistência via localStorage)
 */
(function () {
  'use strict';

  /* ── Busca em tempo real ─────────────────────────── */
  function initSearch() {
    const inputs = [
      document.getElementById('searchProducts'),
      document.getElementById('searchProductsMobile'),
    ].filter(Boolean);

    if (!inputs.length) return;

    const cards   = document.querySelectorAll('#productsGrid .product-card');
    const counter = document.getElementById('resultsCounter');

    function filterCards(query) {
      const q = query.trim().toLowerCase();
      let visible = 0;

      cards.forEach(card => {
        const name = card.dataset.name || '';
        const show = !q || name.includes(q);
        card.style.display = show ? '' : 'none';
        if (show) visible++;
      });

      if (counter) {
        counter.innerHTML = `<strong>${visible}</strong> produto${visible !== 1 ? 's' : ''}`;
      }
    }

    inputs.forEach(input => {
      // Sincroniza os dois inputs
      input.addEventListener('input', e => {
        const val = e.target.value;
        inputs.forEach(other => { if (other !== input) other.value = val; });
        filterCards(val);
      });
    });
  }


  /* ── Mini-slider no hover ────────────────────────── */
  function initSliders() {
    document.querySelectorAll('.product-card').forEach(card => {
      const slides = card.querySelectorAll('.product-slide');
      if (slides.length <= 1) return;

      let idx   = 0;
      let timer = null;

      function showSlide(i) {
        idx = ((i % slides.length) + slides.length) % slides.length;
        const track = card.querySelector('.product-slides');
        if (track) track.style.transform = `translateX(-${idx * 100}%)`;
      }

      card.addEventListener('mouseenter', () => {
        timer = setInterval(() => showSlide(idx + 1), 900);
      });

      card.addEventListener('mouseleave', () => {
        clearInterval(timer);
        showSlide(0);
      });
    });
  }


  /* ── Drawer mobile ───────────────────────────────── */
  function initDrawer() {
    const drawer  = document.getElementById('mobileDrawer');
    const openBtn = document.getElementById('openFilters');
    const closeBtn = document.getElementById('closeFilters');
    const overlay = document.getElementById('drawerOverlay');

    if (!drawer) return;

    function open() {
      drawer.classList.add('open');
      drawer.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    }

    function close() {
      drawer.classList.remove('open');
      drawer.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }

    if (openBtn)  openBtn.addEventListener('click', open);
    if (closeBtn) closeBtn.addEventListener('click', close);
    if (overlay)  overlay.addEventListener('click', close);

    // Fecha com Escape
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') close();
    });
  }


  /* ── Favoritos ───────────────────────────────────── */
  function initFavorites() {
    const KEY = 'pollo_favorites';

    function getFavs() {
      try { return JSON.parse(localStorage.getItem(KEY)) || []; }
      catch { return []; }
    }

    function saveFavs(favs) {
      try { localStorage.setItem(KEY, JSON.stringify(favs)); }
      catch {}
    }

    const favs = getFavs();

    document.querySelectorAll('.favorite-btn').forEach(btn => {
      const id = btn.dataset.id;

      // Restaura estado salvo
      if (favs.includes(id)) btn.classList.add('active');

      btn.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();

        const current = getFavs();
        const i = current.indexOf(id);

        if (i === -1) {
          current.push(id);
          btn.classList.add('active');
        } else {
          current.splice(i, 1);
          btn.classList.remove('active');
        }

        saveFavs(current);
      });
    });
  }


  /* ── Init ────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    initSearch();
    initSliders();
    initDrawer();
    initFavorites();
  });

})();