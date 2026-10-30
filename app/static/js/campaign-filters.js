(() => {
  const form = document.querySelector('[data-auto-client-filters]');
  const results = document.querySelector('[data-client-results]');
  const status = document.querySelector('[data-filter-status]');
  if (!form || !results || !status) return;
  const fallback = form.querySelector('button[type="submit"]');
  if (fallback) fallback.hidden = true;
  let timer, controller, revision = 0;
  const busy = value => {
    results.setAttribute('aria-busy', String(value));
    results.querySelectorAll('button[type="submit"], button:not([type])').forEach(button => button.disabled = value);
  };
  const count = () => `${results.querySelectorAll('input[name="policy_ids"]').length} clients shown`;
  status.textContent = count();
  async function refresh(version) {
    if (!form.checkValidity()) { busy(false); status.textContent = 'Complete a valid date to update clients.'; return; }
    controller = new AbortController();
    const url = new URL(form.action || location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Could not load clients');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.querySelector('[data-client-results]');
      if (!next) throw new Error('Client list unavailable');
      if (version !== revision) return;
      const selected = new Set(Array.from(results.querySelectorAll('input[name="policy_ids"]:checked'), input => input.value));
      results.replaceChildren(...next.childNodes);
      results.querySelectorAll('input[name="policy_ids"]').forEach(input => input.checked = selected.has(input.value));
      results.querySelectorAll('.individual-radio:checked').forEach(input => input.closest('tr').classList.add('recipient-row-selected'));
      // Date-basis changes can expose a different set of years.
      const year = form.elements.namedItem('year');
      const nextYear = page.querySelector('[data-auto-client-filters] select[name="year"]');
      if (year && nextYear) {
        const value = year.value;
        year.replaceChildren(...nextYear.childNodes);
        if (value && !Array.from(year.options).some(option => option.value === value)) year.add(new Option(value, value));
        year.value = value;
      }
      history.replaceState(null, '', url);
      status.textContent = count();
      if (fallback) fallback.hidden = true;
    } catch (error) {
      if (version !== revision || error.name === 'AbortError') return;
      status.textContent = 'Could not update clients. Check your filters and connection, then click Filter to retry.';
      if (fallback) fallback.hidden = false;
    } finally {
      if (version === revision) busy(false);
    }
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (controller) controller.abort();
    const version = ++revision;
    busy(true);
    status.textContent = 'Updating clients…';
    timer = setTimeout(() => refresh(version), delay);
  }
  form.addEventListener('change', () => schedule(150));
  form.addEventListener('input', event => {
    if (event.target.name === 'q' && !event.isComposing) schedule(400);
  });
  form.addEventListener('submit', event => { event.preventDefault(); schedule(0); });
  results.addEventListener('change', event => {
    if (!event.target.matches('.individual-radio')) return;
    results.querySelectorAll('tbody tr').forEach(row => row.classList.remove('recipient-row-selected'));
    event.target.closest('tr').classList.add('recipient-row-selected');
  });
})();
