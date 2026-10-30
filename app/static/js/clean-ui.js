// Open collapsed sections when a document/status link targets content inside them.
function revealLinkedSection() {
  if (!location.hash) return;
  let key;
  try { key = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
  const target = document.getElementById(key);
  if (!target) return;
  let node = target;
  while (node) { if (node.tagName === 'DETAILS') node.open = true; node = node.parentElement; }
  target.scrollIntoView({block:'start'});
}
window.addEventListener('hashchange', revealLinkedSection);
revealLinkedSection();
