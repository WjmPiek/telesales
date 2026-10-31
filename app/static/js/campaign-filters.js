(() => {
  const form = document.querySelector('[data-auto-client-filters]');
  const results = document.querySelector('[data-client-results]');
  const status = document.querySelector('[data-filter-status]');
  if (!form || !results || !status) return;
  const fallback = form.querySelector('button[type="submit"]');
  const batch = document.querySelector('[data-client-batch]');
  const batchCount = document.querySelector('[data-batch-count]');
  const delivery = document.querySelector('[data-campaign-delivery]');
  if (fallback) fallback.hidden = true;
  let timer, controller, revision = 0;
  let queue = form.dataset.campaignQueue;
  const selectionCount = () => results.querySelectorAll('input[name="policy_ids"]:checked:not(:disabled)').length;
  function updateSelection() {
    if (batchCount) batchCount.textContent = `${selectionCount()} selected`;
    if (results.getAttribute('aria-busy') !== 'true') busy(false);
  }
  function applyBatch() {
    if (batch && batch.value !== 'manual') {
      const inputs = Array.from(results.querySelectorAll('input[name="policy_ids"]:not(:disabled)'));
      inputs.forEach((input, index) => input.checked = index < Number(batch.value));
    }
    updateSelection();
  }
  const busy = value => {
    results.setAttribute('aria-busy', String(value));
    const sending = ['queued','retry','processing'].includes(queue);
    const approved = document.getElementById('sendCampaignButton')?.dataset.approved !== 'false';
    results.querySelectorAll('button[type="submit"], button:not([type])').forEach(button => button.disabled = value || !selectionCount() || (Boolean(results.querySelector('[data-batch-send-form]')) && (sending || !approved)));
    document.querySelectorAll('form[data-selection-action] button').forEach(button => {
      button.disabled = value || !selectionCount() || sending || !approved;
      button.textContent = button.form.dataset.selectionAction === 'send' ? `Send ${selectionCount() || ''} selected now` : `Schedule ${selectionCount() || ''} selected`;
    });
  };
  const count = () => `${results.querySelectorAll('input[name="policy_ids"]').length} clients shown`;
  status.textContent = count();
  async function refresh(version, polling) {
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
      results.querySelectorAll('input[name="policy_ids"]').forEach(input => input.checked = !input.disabled && selected.has(input.value));
      results.querySelectorAll('.individual-radio:checked').forEach(input => input.closest('tr').classList.add('recipient-row-selected'));
      const nextDelivery = page.querySelector('[data-campaign-delivery]');
      if (delivery && nextDelivery) delivery.replaceChildren(...nextDelivery.childNodes);
      queue = page.querySelector('[data-auto-client-filters]')?.dataset.campaignQueue;
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
      if (!polling) applyBatch();
      else updateSelection();
      if (fallback) fallback.hidden = true;
    } catch (error) {
      if (version !== revision || error.name === 'AbortError') return;
      status.textContent = 'Could not update clients. Check your filters and connection, then click Filter to retry.';
      if (fallback) fallback.hidden = false;
    } finally {
      if (version === revision) busy(false);
    }
  }
  function schedule(delay, polling = false) {
    clearTimeout(timer);
    if (controller) controller.abort();
    const version = ++revision;
    busy(true);
    status.textContent = 'Updating clients…';
    timer = setTimeout(() => refresh(version, polling), delay);
  }
  form.addEventListener('change', () => schedule(150));
  form.addEventListener('input', event => {
    if (event.target.name === 'q' && !event.isComposing) schedule(400);
  });
  form.addEventListener('submit', event => { event.preventDefault(); schedule(0); });
  results.addEventListener('change', event => {
    if (!event.target.matches('input[name="policy_ids"]')) return;
    if (batch) batch.value = 'manual';
    if (event.target.matches('.individual-radio')) {
      results.querySelectorAll('tbody tr').forEach(row => row.classList.remove('recipient-row-selected'));
      event.target.closest('tr').classList.add('recipient-row-selected');
    }
    updateSelection();
  });
  if (batch) batch.addEventListener('change', () => {
    if (batch.value === 'manual') results.querySelectorAll('input[name="policy_ids"]').forEach(input => input.checked = false);
    applyBatch();
  });
  document.addEventListener('campaign-template-status', updateSelection);
  document.addEventListener('DOMContentLoaded', updateSelection);
  document.addEventListener('submit', event => {
    const target = event.target;
    if (!target.matches('[data-batch-send-form], [data-selection-action]')) return;
    const inputs = Array.from(results.querySelectorAll('input[name="policy_ids"]:checked:not(:disabled)'));
    if (!inputs.length) { event.preventDefault(); status.textContent = 'Choose clients before sending or scheduling.'; return; }
    const action = target.dataset.selectionAction || event.submitter?.value || 'send';
    if (action === 'send' && !confirm(`Send this campaign to these ${inputs.length} selected client(s) now?`)) { event.preventDefault(); return; }
    if (target.hasAttribute('data-selection-action')) {
      target.querySelectorAll('input[data-selection-copy]').forEach(input => input.remove());
      for (const [name, value] of [...inputs.map(input => ['policy_ids', input.value]), ['selection_action', action]]) {
        const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; input.dataset.selectionCopy = '1'; target.append(input);
      }
    }
  });
  busy(false);
  // Refresh delivery colours while the background sender works. This is read-only.
  setInterval(() => {
    if (!document.hidden && ['queued','processing','retry'].includes(queue) && results.getAttribute('aria-busy') !== 'true') schedule(0, true);
  }, 5000);
})();
