(function () {
  document.documentElement.classList.add('js');
  let saved;
  try { saved = localStorage.getItem('kamiwa-theme'); } catch (_) {}
  document.documentElement.dataset.theme = saved === 'light' ? 'light' : 'dark';
})();
