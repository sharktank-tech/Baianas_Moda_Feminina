document.addEventListener('DOMContentLoaded', () => {
  const slides = document.querySelector('.promo-slides');
  const prevBtn = document.querySelector('.promo-prev');
  const nextBtn = document.querySelector('.promo-next');

  let currentIndex = 0;
  const totalSlides = slides.children.length;
  const visibleSlides = 3; // Exibe 3 slides por vez
  const slideWidth = slides.children[0].offsetWidth;

  const updateSlidePosition = () => {
    slides.style.transform = `translateX(-${currentIndex * slideWidth}px)`;
  };

  const goToNextSlide = () => {
    if (currentIndex < totalSlides - visibleSlides) {
      currentIndex++;
    } else {
      currentIndex = 0; // Retorna ao início se estiver no último slide
    }
    updateSlidePosition();
  };

  let autoSlideInterval = setInterval(goToNextSlide, 5000);

  prevBtn.addEventListener('click', () => {
    clearInterval(autoSlideInterval); // Pausa o auto slide
    if (currentIndex > 0) {
      currentIndex--;
      updateSlidePosition();
    }
    autoSlideInterval = setInterval(goToNextSlide, 5000); // Reinicia o auto slide
  });

  nextBtn.addEventListener('click', () => {
    clearInterval(autoSlideInterval); // Pausa o auto slide
    if (currentIndex < totalSlides - visibleSlides) {
      currentIndex++;
      updateSlidePosition();
    }
    autoSlideInterval = setInterval(goToNextSlide, 5000); // Reinicia o auto slide
  });
});
