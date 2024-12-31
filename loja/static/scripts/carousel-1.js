document.addEventListener('DOMContentLoaded', () => {
  const slides = document.querySelector('.slides');
  const slidesCount = document.querySelectorAll('.slide').length;
  const prevButton = document.querySelector('.prev');
  const nextButton = document.querySelector('.next');

  let currentIndex = 0;

  const updateCarousel = (isFastTransition = false) => {
    // Define a transição acelerada se necessário
    if (isFastTransition) {
      slides.style.transition = 'transform 0.1s ease-in-out'; // Transição mais rápida
    } else {
      slides.style.transition = 'transform 1s ease-in-out'; // Transição padrão
    }

    slides.style.transform = `translateX(-${currentIndex * 100}%)`;
  };

  prevButton.addEventListener('click', () => {
    currentIndex = (currentIndex > 0) ? currentIndex - 1 : slidesCount - 1;
    updateCarousel(currentIndex === slidesCount - 1); // Transição rápida se for do primeiro para o último
  });

  nextButton.addEventListener('click', () => {
    currentIndex = (currentIndex < slidesCount - 1) ? currentIndex + 1 : 0;
    updateCarousel(currentIndex === 0); // Transição rápida se for do último para o primeiro
  });

  // Auto-slide a cada 5 segundos
  setInterval(() => {
    const isLastSlide = currentIndex === slidesCount - 1;
    currentIndex = (currentIndex < slidesCount - 1) ? currentIndex + 1 : 0;
    updateCarousel(isLastSlide);
  }, 5000);
});