// Gerenciar slides do carrossel
document.addEventListener('DOMContentLoaded', () => {
  const slides = document.querySelector('.slides');
  const slidesCount = document.querySelectorAll('.slide').length;
  const prevButton = document.querySelector('.prev');
  const nextButton = document.querySelector('.next');

  let currentIndex = 0;

  const updateCarousel = () => {
    slides.style.transform = `translateX(-${currentIndex * 100}%)`;
  };

  prevButton.addEventListener('click', () => {
    currentIndex = (currentIndex > 0) ? currentIndex - 1 : slidesCount - 1;
    updateCarousel();
  });

  nextButton.addEventListener('click', () => {
    currentIndex = (currentIndex < slidesCount - 1) ? currentIndex + 1 : 0;
    updateCarousel();
  });

  // Auto-slide a cada 5 segundos
  setInterval(() => {
    currentIndex = (currentIndex < slidesCount - 1) ? currentIndex + 1 : 0;
    updateCarousel();
  }, 5000);
});