document.querySelectorAll('.mobile-document-picker').forEach(picker => {
  const inputs = [...picker.querySelectorAll('input[type="file"]')];
  const normal = inputs[0];
  picker.querySelector('.camera-trigger').addEventListener('click', event => {
    document.getElementById(event.currentTarget.dataset.cameraId).click();
  });
  inputs.forEach(input => input.addEventListener('change', () => {
    if (input.files.length) inputs.filter(other => other !== input).forEach(other => { other.value = ''; });
    const chosen = inputs.find(candidate => candidate.files.length);
    normal.required = normal.dataset.originalRequired === '1' && !chosen;
    picker.querySelector('.selected-document').textContent = chosen ? 'Selected: ' + chosen.files[0].name : '';
  }));
});
