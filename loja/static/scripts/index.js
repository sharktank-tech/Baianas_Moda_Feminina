/**
 * index.js — Pollo Modas
 * Controla o Product Banner carousel principal:
 *   • navegação prev/next/dots
 *   • autoplay com pausa no hover
 *   • mini-slider de imagens (hover na img-side)
 *   • swipe touch
 */
(function () {
  'use strict';

  const track  = document.getElementById('pbTrack');
  const dotsEl = document.getElementById('pbDots');

  if (!track) return; // página sem banner, sai sem erros

  const dots   = dotsEl ? dotsEl.querySelectorAll('.pb-dot') : [];
  const slides = track.querySelectorAll('.pb-slide');
  const total  = slides.length;
  let current  = 0;
  let timer    = null;

  // Oculta botões se só existe 1 slide
  if (total <= 1) {
    const prevBtn = document.getElementById('pbPrev');
    const nextBtn = document.getElementById('pbNext');
    if (prevBtn) prevBtn.hidden = true;
    if (nextBtn) nextBtn.hidden = true;
    if (dotsEl)  dotsEl.hidden  = true;
  }

  /* ── Navega para o slide i ─────────────────────────── */
  function goTo(i) {
    current = ((i % total) + total) % total;
    track.style.transform = `translateX(-${current * 100}%)`;

    dots.forEach((d, idx) => {
      d.classList.toggle('active', idx === current);
    });

    // Reseta mini-slider de imagens de todos os slides
    slides.forEach(slide => {
      slide.querySelectorAll('.pb-img').forEach((img, j) =>
        img.classList.toggle('active', j === 0)
      );
      slide.querySelectorAll('.pb-img-dot').forEach((d, j) =>
        d.classList.toggle('active', j === 0)
      );
    });

    resetTimer();
  }

  /* ── Autoplay ──────────────────────────────────────── */
  function startTimer() {
    if (total > 1) timer = setInterval(() => goTo(current + 1), 6000);
  }

  function resetTimer() {
    clearInterval(timer);
    startTimer();
  }

  /* ── Botões prev / next ────────────────────────────── */
  const prevBtn = document.getElementById('pbPrev');
  const nextBtn = document.getElementById('pbNext');
  if (prevBtn) prevBtn.addEventListener('click', () => goTo(current - 1));
  if (nextBtn) nextBtn.addEventListener('click', () => goTo(current + 1));

  /* ── Dots ──────────────────────────────────────────── */
  dots.forEach((d, i) => d.addEventListener('click', () => goTo(i)));

  /* ── Mini-slider de imagens (hover na img-side) ────── */
  slides.forEach(slide => {
    const imgs  = slide.querySelectorAll('.pb-img');
    const idots = slide.querySelectorAll('.pb-img-dot');
    if (imgs.length <= 1) return;

    let imgIdx   = 0;
    let imgTimer = null;

    function showImg(i) {
      imgIdx = ((i % imgs.length) + imgs.length) % imgs.length;
      imgs.forEach((img, j)  => img.classList.toggle('active', j === imgIdx));
      idots.forEach((d, j) => d.classList.toggle('active', j === imgIdx));
    }

    const imgSide = slide.querySelector('.pb-img-side');
    if (!imgSide) return;

    imgSide.addEventListener('mouseenter', () => {
      imgTimer = setInterval(() => showImg(imgIdx + 1), 1100);
    });
    imgSide.addEventListener('mouseleave', () => {
      clearInterval(imgTimer);
      showImg(0);
    });
  });

  /* ── Swipe touch ───────────────────────────────────── */
  let touchStartX = null;

  track.addEventListener('touchstart', e => {
    touchStartX = e.touches[0].clientX;
  }, { passive: true });

  track.addEventListener('touchend', e => {
    if (touchStartX === null) return;
    const diff = touchStartX - e.changedTouches[0].clientX;
    if (Math.abs(diff) > 44) goTo(current + (diff > 0 ? 1 : -1));
    touchStartX = null;
  }, { passive: true });

  /* ── Pausa no hover (banner inteiro) ───────────────── */
  const banner = track.closest('.pb-banner');
  if (banner) {
    banner.addEventListener('mouseenter', () => clearInterval(timer));
    banner.addEventListener('mouseleave', () => {
      clearInterval(timer);
      startTimer();
    });
  }

  /* ── Pausa se aba está oculta ──────────────────────── */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearInterval(timer);
    else startTimer();
  });

  /* ── Inicia ────────────────────────────────────────── */
  startTimer();
})();