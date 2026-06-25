/* ═══════════════════════════════════════════════════════════
   carousel-1.js — Hero carousel
   + Mini carrossel de imagens nos cards de produto (hover)
═══════════════════════════════════════════════════════════ */

document.addEventListener("DOMContentLoaded", () => {

  // ── Hero carousel ────────────────────────────────────────
  const carousel = document.querySelector("#carousel1");
  if (!carousel) return;

  const slidesContainer = carousel.querySelector(".slides");
  const slides          = carousel.querySelectorAll(".slide");
  const prevButton      = carousel.querySelector(".prev");
  const nextButton      = carousel.querySelector(".next");
  const dots            = carousel.querySelectorAll(".dot");

  const totalSlides = slides.length;
  if (totalSlides <= 1) return;

  let currentIndex = 0;
  let autoSlide;

  // Transição padrão vs. rápida (wrap do último → primeiro)
  const TRANSITION_NORMAL = "transform 1s ease-in-out";
  const TRANSITION_FAST   = "transform 0.1s ease-in-out";

  function updateCarousel(fast = false) {
    slidesContainer.style.transition = fast ? TRANSITION_FAST : TRANSITION_NORMAL;
    slidesContainer.style.transform  = `translateX(-${currentIndex * 100}%)`;

    // Sincroniza dots
    dots.forEach((dot, i) => dot.classList.toggle("active", i === currentIndex));

    // Reativa animação do label
    const label = slides[currentIndex].querySelector(".slide-label");
    if (label) {
      label.style.animation = "none";
      void label.offsetWidth;
      label.style.animation = "";
    }
  }

  function nextSlide() {
    const isLast = currentIndex === totalSlides - 1;
    currentIndex = isLast ? 0 : currentIndex + 1;
    updateCarousel(isLast); // rápido ao fazer wrap último → primeiro
  }

  function prevSlide() {
    const isFirst = currentIndex === 0;
    currentIndex  = isFirst ? totalSlides - 1 : currentIndex - 1;
    updateCarousel(isFirst); // rápido ao fazer wrap primeiro → último
  }

  function startAutoSlide() {
    autoSlide = setInterval(nextSlide, 5000);
  }

  function stopAutoSlide() {
    clearInterval(autoSlide);
  }

  // ── Eventos dos botões ───────────────────────────────────
  nextButton.addEventListener("click", () => {
    stopAutoSlide();
    nextSlide();
    startAutoSlide();
  });

  prevButton.addEventListener("click", () => {
    stopAutoSlide();
    prevSlide();
    startAutoSlide();
  });

  // ── Dots ─────────────────────────────────────────────────
  dots.forEach((dot, i) => {
    dot.addEventListener("click", () => {
      stopAutoSlide();
      const fast   = Math.abs(i - currentIndex) === totalSlides - 1;
      currentIndex = i;
      updateCarousel(fast);
      startAutoSlide();
    });
  });

  // ── Pausa ao hover ───────────────────────────────────────
  carousel.addEventListener("mouseenter", stopAutoSlide);
  carousel.addEventListener("mouseleave", startAutoSlide);

  // ── Touch swipe ──────────────────────────────────────────
  let touchStartX = 0;
  let touchDeltaX = 0;

  carousel.addEventListener("touchstart", (e) => {
    touchStartX = e.touches[0].clientX;
    stopAutoSlide();
  }, { passive: true });

  carousel.addEventListener("touchmove", (e) => {
    touchDeltaX = e.touches[0].clientX - touchStartX;
  }, { passive: true });

  carousel.addEventListener("touchend", () => {
    if      (touchDeltaX < -50) nextSlide();
    else if (touchDeltaX >  50) prevSlide();
    touchDeltaX = 0;
    startAutoSlide();
  });

  // ── Teclado ──────────────────────────────────────────────
  carousel.setAttribute("tabindex", "0");
  carousel.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft")  { stopAutoSlide(); prevSlide(); startAutoSlide(); }
    if (e.key === "ArrowRight") { stopAutoSlide(); nextSlide(); startAutoSlide(); }
  });

  updateCarousel();
  startAutoSlide();


  // ── Mini carrossel de imagens nos cards (hover) ──────────
  document.querySelectorAll(".product-img-wrap").forEach((wrap) => {
    const imgs       = wrap.querySelectorAll(".product-img");
    const indicators = wrap.querySelectorAll(".img-indicator");
    const total      = imgs.length;

    if (total <= 1) return;

    let current = 0;

    function goTo(index) {
      imgs[current].classList.remove("active");
      indicators[current]?.classList.remove("active");
      current = index;
      imgs[current].classList.add("active");
      indicators[current]?.classList.add("active");
    }

    wrap.addEventListener("mousemove", (e) => {
      const rect  = wrap.getBoundingClientRect();
      const zone  = rect.width / total;
      const index = Math.min(Math.floor((e.clientX - rect.left) / zone), total - 1);
      if (index !== current) goTo(index);
    });

    wrap.addEventListener("mouseleave", () => goTo(0));
  });

});