(() => {
    const form = document.getElementById('client-import');
    const dialog = document.getElementById('import-progress');
    if (!form || typeof dialog?.showModal !== 'function' || typeof XMLHttpRequest === 'undefined') return;

    const submit = form.querySelector('button[type="submit"], button:not([type])');
    const title = document.getElementById('import-progress-title');
    const status = document.getElementById('import-progress-status');
    const bar = document.getElementById('import-progress-bar');
    const openClients = document.getElementById('import-progress-open');
    const close = document.getElementById('import-progress-close');
    const mediaType = 'application/x-ndjson';
    const interrupted = 'The connection ended before Telesales confirmed the result. Check Saved Files before trying again.';
    const number = new Intl.NumberFormat();
    let running = false;
    let saved = false;

    function showProgress(message, value, total) {
        status.textContent = message;
        bar.hidden = false;
        if (total > 0) {
            bar.max = total;
            bar.value = value;
        } else {
            bar.removeAttribute('value');
        }
    }

    function finish(message, location, confirmedFailure = false) {
        running = false;
        saved = Boolean(location);
        submit.disabled = saved;
        if (saved) {
            title.textContent = 'Import complete';
        } else {
            title.textContent = confirmedFailure ? 'Import failed' : 'Import could not be confirmed';
        }
        status.textContent = message;
        bar.hidden = !saved;
        if (saved) {
            bar.max = 1;
            bar.value = 1;
            openClients.href = location;
        }
        openClients.hidden = !saved;
        close.hidden = false;
        (saved ? openClients : close).focus();
    }

    function handleEvent(event) {
        switch (event.stage) {
        case 'checking':
            showProgress('Checking the workbook and removing duplicate mobile numbers…');
            break;
        case 'reading_clients':
            showProgress('Checking names and mobile numbers…');
            break;
        case 'processing':
            showProgress(
                number.format(event.processed) + ' of ' + number.format(event.total) + ' contacts processed',
                event.processed, event.total
            );
            break;
        case 'saving':
            showProgress('Saving the file and client records…');
            break;
        case 'done':
            finish(event.message, event.url);
            break;
        case 'error':
            finish(event.message, null, true);
            break;
        }
    }

    dialog.addEventListener('cancel', event => {
        if (running) event.preventDefault();
    });
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
        if (saved) window.location.reload();
    });

    form.addEventListener('submit', event => {
        event.preventDefault();
        if (running || saved) return;

        const request = new XMLHttpRequest();
        const data = new FormData(form);
        let offset = 0;
        running = true;
        submit.disabled = true;
        openClients.hidden = true;
        close.hidden = true;
        title.textContent = 'Importing clients';
        showProgress('Uploading workbook…');
        dialog.showModal();

        function consumeEvents() {
            if (!running || !(request.getResponseHeader('Content-Type') || '').startsWith(mediaType)) return;
            const body = request.responseText;
            let end = body.indexOf('\n', offset);
            while (end !== -1 && running) {
                try {
                    handleEvent(JSON.parse(body.slice(offset, end)));
                } catch {
                    finish(interrupted);
                }
                offset = end + 1;
                end = body.indexOf('\n', offset);
            }
        }

        request.upload.addEventListener('progress', progress => {
            if (progress.lengthComputable && progress.total > 0) {
                const percent = Math.floor(progress.loaded / progress.total * 100);
                showProgress('Uploading workbook… ' + percent + '%', progress.loaded, progress.total);
            }
        });
        request.upload.addEventListener('load', () => showProgress('Checking the workbook…'));
        request.addEventListener('progress', consumeEvents);
        request.addEventListener('load', () => {
            consumeEvents();
            if (!running) return;
            if (request.status === 413) {
                finish('The workbook must be 10 MB or smaller.', null, true);
            } else if ([400, 401, 403].includes(request.status) || request.responseURL.includes('/login')) {
                finish('Refresh the import page and sign in again if prompted.', null, true);
            } else {
                finish(interrupted);
            }
        });
        request.addEventListener('error', () => finish(interrupted));
        request.addEventListener('abort', () => finish(interrupted));
        request.open('POST', form.action);
        request.setRequestHeader('Accept', mediaType);
        request.send(data);
    });
})();
