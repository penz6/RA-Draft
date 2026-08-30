(function(){
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Auto-dismiss flashes after 6 seconds if not reduced-motion
  const flashes = document.querySelectorAll('.flash');
  if (flashes.length > 0 && !prefersReducedMotion) {
    setTimeout(() => {
      flashes.forEach(f => {
        f.style.transition = 'opacity 0.3s ease';
        f.style.opacity = '0';
        setTimeout(() => f.remove(), 300);
      });
    }, 6000);
  }

  // Smooth scroll helper
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function(e) {
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({
          behavior: prefersReducedMotion ? 'auto' : 'smooth'
        });
      }
    });
  });
})();
