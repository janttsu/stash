// Applied before first paint so a saved dark/light choice does not flash. Kept as a file: the CSP forbids inline scripts.
try {
  document.documentElement.dataset.theme = localStorage.getItem('stash.theme') || 'auto';
} catch (e) {
  document.documentElement.dataset.theme = 'auto';
}
