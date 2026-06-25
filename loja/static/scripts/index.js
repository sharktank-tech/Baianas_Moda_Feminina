(function () {
  const track   = document.getElementById('pbTrack');
  const dots    = document.querySelectorAll('#pbDots .pb-dot');
  const slides  = track.querySelectorAll('.pb-slide');
  const total   = slides.length;
  let   current = 0;
  let   timer   = null;

  if (total <= 1) {
    document.getElementById('pbPrev').style.display = 'none';
    document.getElementById('pbNext').style.display = 'none';
  }

  /* ── Navega para slide i ──────────────────────────────── */
  function goTo(i) {
    current = (i + total) % total;
    track.style.transform = `translateX(-${current * 100}%)`;

    dots.forEach((d, idx) => d.classList.toggle('active', idx === current));

    /* mini-slider de imagens: reseta para imagem 1 */
    slides.forEach(slide => {
      const imgs  = slide.querySelectorAll('.pb-img');
      const idots = slide.querySelectorAll('.pb-img-dot');
      imgs.forEach((img, j) => img.classList.toggle('active', j === 0));
      idots.forEach((d, j) => d.classList.toggle('active', j === 0));
    });

    resetTimer();
  }

  /* ── Autoplay ─────────────────────────────────────────── */
  function resetTimer() {
    clearInterval(timer);
    if (total > 1) timer = setInterval(() => goTo(current + 1), 6000);
  }

  document.getElementById('pbPrev').addEventListener('click', () => goTo(current - 1));
  document.getElementById('pbNext').addEventListener('click', () => goTo(current + 1));
  dots.forEach((d, i) => d.addEventListener('click', () => goTo(i)));

  /* ── Mini-slider de imagens (hover / swipe tap) ───────── */
  slides.forEach(slide => {
    const imgs  = slide.querySelectorAll('.pb-img');
    const idots = slide.querySelectorAll('.pb-img-dot');
    if (imgs.length <= 1) return;

    let imgIdx = 0;

    function showImg(i) {
      imgIdx = (i + imgs.length) % imgs.length;
      imgs.forEach((img, j) => img.classList.toggle('active', j === imgIdx));
      idots.forEach((d,  j) => d.classList.toggle('active',  j === imgIdx));
    }

    /* avança imagem ao passar o mouse sobre a metade direita */
    let imgTimer = null;
    slide.querySelector('.pb-img-side').addEventListener('mouseenter', () => {
      imgTimer = setInterval(() => showImg(imgIdx + 1), 1200);
    });
    slide.querySelector('.pb-img-side').addEventListener('mouseleave', () => {
      clearInterval(imgTimer);
      showImg(0);
    });
  });

  /* ── Swipe touch no banner principal ─────────────────── */
  let touchX = null;
  track.addEventListener('touchstart', e => { touchX = e.touches[0].clientX; }, { passive: true });
  track.addEventListener('touchend',   e => {
    if (touchX === null) return;
    const diff = touchX - e.changedTouches[0].clientX;
    if (Math.abs(diff) > 40) goTo(current + (diff > 0 ? 1 : -1));
    touchX = null;
  }, { passive: true });

  /* ── Pausa no hover ───────────────────────────────────── */
  track.closest('.pb-banner').addEventListener('mouseenter', () => clearInterval(timer));
  track.closest('.pb-banner').addEventListener('mouseleave', resetTimer);

  resetTimer();
})();