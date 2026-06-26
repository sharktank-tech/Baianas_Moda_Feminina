/* ═══════════════════════════════════════════════════════════
   carousel-1.js — Hero carousel + mini carrossel de produto
═══════════════════════════════════════════════════════════ */

document.addEventListener("DOMContentLoaded", () => {

  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ─────────────────────────────────────────────────────────
  // HERO CAROUSEL
  // ─────────────────────────────────────────────────────────
  initHeroCarousel();

  function initHeroCarousel() {
    const carousel = document.querySelector("#carousel1");
    if (!carousel) return;

    const track      = carousel.querySelector(".slides");
    const slides     = carousel.querySelectorAll(".slide");
    const prevBtn    = carousel.querySelector(".prev");
    const nextBtn    = carousel.querySelector(".next");
    const dots       = carousel.querySelectorAll(".dot");
    const total      = slides.length;

    if (total <= 1) {
      prevBtn?.remove();
      nextBtn?.remove();
      return;
    }

    let index     = 0;
    let timer     = null;
    let animating = false;  // guarda contra cliques durante transição

    // ── Navegação ───────────────────────────────────────────
    function goTo(next, { fast = false } = {}) {
      if (animating && !fast) return;
      animating = true;

      const duration = (reduceMotion || fast) ? 0 : 700;
      track.style.transition = duration ? `transform ${duration}ms cubic-bezier(0.77,0,0.18,1)` : "none";
      track.style.transform  = `translateX(-${next * 100}%)`;

      // atualiza dots
      dots.forEach((d, i) => d.classList.toggle("active", i === next));

      // reanima label
      if (!reduceMotion) {
        const label = slides[next].querySelector(".slide-label");
        if (label) {
          label.style.animation = "none";
          void label.offsetWidth;
          label.style.animation = "";
        }
      }

      index = next;

      // libera guarda após a transição
      setTimeout(() => { animating = false; }, duration + 50);
    }

    function next() { goTo((index + 1) % total); }
    function prev() { goTo((index - 1 + total) % total); }

    // ── Autoplay ────────────────────────────────────────────
    function startAuto() {
      stopAuto();                         // garante que não há timer duplo
      if (!reduceMotion) timer = setInterval(next, 6000);
    }
    function stopAuto() {
      clearInterval(timer);
      timer = null;
    }

    // ── Eventos — botões ────────────────────────────────────
    nextBtn?.addEventListener("click", () => { next(); startAuto(); });
    prevBtn?.addEventListener("click", () => { prev(); startAuto(); });

    // ── Eventos — dots ──────────────────────────────────────
    dots.forEach((dot, i) => {
      dot.addEventListener("click", () => {
        if (i === index) return;
        // wrap de ponta a ponta → fast para evitar longa travessia visual
        const isWrap = Math.abs(i - index) === total - 1;
        goTo(i, { fast: isWrap });
        startAuto();
      });
    });

    // ── Eventos — hover ─────────────────────────────────────
    carousel.addEventListener("mouseenter", stopAuto);
    carousel.addEventListener("mouseleave", startAuto);

    // ── Eventos — touch swipe ───────────────────────────────
    let touchX = null;

    carousel.addEventListener("touchstart", (e) => {
      touchX = e.touches[0].clientX;
      stopAuto();
    }, { passive: true });

    carousel.addEventListener("touchend", (e) => {
      if (touchX === null) return;
      const delta = e.changedTouches[0].clientX - touchX;
      touchX = null;
      if      (delta < -50) next();
      else if (delta >  50) prev();
      startAuto();
    }, { passive: true });

    // ── Eventos — teclado ───────────────────────────────────
    carousel.setAttribute("tabindex", "0");
    carousel.setAttribute("aria-roledescription", "carrossel");
    carousel.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft")  { prev(); startAuto(); }
      if (e.key === "ArrowRight") { next(); startAuto(); }
    });

    // ── Visibilidade — pausa quando a aba está oculta ───────
    document.addEventListener("visibilitychange", () => {
      document.hidden ? stopAuto() : startAuto();
    });

    goTo(0, { fast: true });
    startAuto();
  }


  // ─────────────────────────────────────────────────────────
  // MINI CARROSSEL DE IMAGENS NOS CARDS (hover por zona)
  // ─────────────────────────────────────────────────────────
  initProductMiniSliders();

  function initProductMiniSliders() {
    document.querySelectorAll(".product-img-wrap").forEach((wrap) => {
      const imgs       = wrap.querySelectorAll(".product-img");
      const indicators = wrap.querySelectorAll(".img-indicator");
      const total      = imgs.length;

      if (total <= 1) return;

      // garante estado inicial correto
      imgs[0].classList.add("active");
      indicators[0]?.classList.add("active");

      let current  = 0;
      let lastZone = -1;   // evita reclacular quando o mouse não mudou de zona

      function goTo(next) {
        if (next === current) return;
        imgs[current].classList.remove("active");
        indicators[current]?.classList.remove("active");
        current = next;
        imgs[current].classList.add("active");
        indicators[current]?.classList.add("active");
      }

      // ── Hover — divide a largura do wrap em zonas ─────────
      wrap.addEventListener("mousemove", (e) => {
        const { left, width } = wrap.getBoundingClientRect();
        const zone = Math.min(Math.floor((e.clientX - left) / (width / total)), total - 1);
        if (zone === lastZone) return;   // sem mudança de zona → ignora
        lastZone = zone;
        goTo(zone);
      });

      wrap.addEventListener("mouseleave", () => {
        lastZone = -1;
        goTo(0);
      });

      // ── Touch — swipe lateral ─────────────────────────────
      let touchX = null;

      wrap.addEventListener("touchstart", (e) => {
        touchX = e.touches[0].clientX;
      }, { passive: true });

      wrap.addEventListener("touchend", (e) => {
        if (touchX === null) return;
        const delta = e.changedTouches[0].clientX - touchX;
        touchX = null;
        if      (delta < -30) goTo(Math.min(current + 1, total - 1));
        else if (delta >  30) goTo(Math.max(current - 1, 0));
      }, { passive: true });
    });
  }

});