'use strict';
window.addEventListener('DOMContentLoaded', async () => {
    const $ = id => document.getElementById(id);
    let reportVisible = false;
    let predictionRequested = false;
    let refreshBusy = false;
    const message = (text, error = false) => {
        $('app-message').textContent = text;
        $('app-message').className = error ? 'message-error' : 'message-success';
    };
    async function api(path, data) {
        try { return await ParkingAPI.request(path, data); }
        catch (error) {
            if (error.status === 401) window.location.replace('login.html');
            throw error;
        }
    }
    try {
        await ParkingAPI.health();
        const me = await api('/api/me');
        document.querySelector('.welcome-bar').textContent = `Welcome, ${me.user}`;
    } catch (error) { message(error.message, true); return; }

    for (const [groupId, inputId] of [['vehicle-group', 'vehicle'], ['personnel-group', 'personnel']]) {
        $(groupId).querySelectorAll('button').forEach(button => {
            button.setAttribute('aria-pressed', 'false');
            button.addEventListener('click', () => {
                $(groupId).querySelectorAll('button').forEach(other => {
                    other.classList.remove('selected'); other.setAttribute('aria-pressed', 'false');
                });
                button.classList.add('selected'); button.setAttribute('aria-pressed', 'true');
                $(inputId).value = button.dataset.value;
            });
        });
    }
    function makeCard(title, value, details) {
        const card = document.createElement('div'); card.className = 'slot-card';
        const heading = document.createElement('h3'); heading.textContent = title;
        const amount = document.createElement('strong'); amount.className = 'availability-number'; amount.textContent = value;
        const text = document.createElement('p'); text.textContent = details;
        card.append(heading, amount, text);
        return card;
    }
    async function loadAvailability() {
        try {
            const result = await api('/availability');
            $('availability-cards').replaceChildren(...result.slots.map(slot =>
                makeCard(slot.vehicle, `${slot.available} available`, `${slot.occupied} of ${slot.capacity} spaces occupied`)));
        } catch (error) {
            $('availability-cards').textContent = 'Availability unavailable. Check the server and refresh.';
            throw error;
        }
    }
    async function loadPrediction() {
        const button = $('predict-btn'); button.disabled = true;
        try {
            const result = await api('/predict');
            $('prediction-cards').replaceChildren(...result.predictions.map(item =>
                makeCard(item.vehicle, item.predictedAvailable === null ? 'Learning' : `About ${item.predictedAvailable} available`,
                    `${item.message} Completed visits: ${item.completedSamples}.`)));
        } catch (error) {
            $('prediction-cards').textContent = 'Prediction unavailable. Please retry.';
            throw error;
        } finally { button.disabled = false; }
    }
    async function renderReport() {
        const params = new URLSearchParams({date: $('filter-date').value, vehicleNo: $('filter-vehicle-no').value.trim()});
        try {
            const result = await api(`/report?${params}`);
            const body = $('report-table-body'); body.replaceChildren();
            $('summary-count').textContent = result.count;
            document.querySelector('a[href^="/excel"]').href = `/excel?${params}`;
            for (const item of result.data) {
                const row = body.insertRow();
                for (const key of ['date', 'vehicle', 'personnel', 'passengers', 'vehicleNo', 'status', 'remarks']) {
                    row.insertCell().textContent = item[key];
                }
                const action = row.insertCell();
                if (item.status === 'Parked') {
                    const button = document.createElement('button'); button.textContent = 'Mark Exit';
                    button.setAttribute('aria-label', `Mark exit for ${item.vehicleNo}`);
                    button.addEventListener('click', async () => {
                        button.disabled = true;
                        try {
                            await api('/exit', {id: item.id});
                            message(`${item.vehicleNo} marked as exited. Parking space released.`);
                            clearRecommendation();
                            await refresh();
                        } catch (error) { message(error.message, true); }
                        finally { button.disabled = false; }
                    });
                    action.appendChild(button);
                } else { action.textContent = 'Completed'; }
            }
            if (!result.count) {
                const cell = body.insertRow().insertCell(); cell.colSpan = 8; cell.textContent = 'No matching records.';
            }
        } catch (error) {
            $('report-table-body').replaceChildren();
            const cell = $('report-table-body').insertRow().insertCell();
            cell.colSpan = 8; cell.textContent = 'Report unavailable. Please retry.';
            $('summary-count').textContent = '—';
            throw error;
        }
    }
    async function refresh() {
        // Let mutation refreshes run even if an automatic refresh is in progress.
        const results = await Promise.allSettled([loadAvailability(), ...(reportVisible ? [renderReport()] : []),
                                                ...(predictionRequested ? [loadPrediction()] : [])]);
        const failure = results.find(result => result.status === 'rejected');
        if (failure) message(failure.reason.message || 'Cannot connect to server.', true);
    }
    $('data-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (!$('vehicle').value || !$('personnel').value) {
            message('Please select a vehicle type and personnel type.', true); return;
        }
        const button = event.submitter || document.querySelector('.btn-submit'); button.disabled = true;
        try {
            await api('/submit', {vehicle: $('vehicle').value, personnel: $('personnel').value,
                passengers: Number($('passengers').value), vehicleNo: $('vehicleNo').value, remarks: $('remarks').value});
            $('data-form').reset(); $('success-modal').classList.remove('hidden');
            message('Vehicle parked successfully.'); clearRecommendation(); await refresh();
        } catch (error) { message(error.message || 'Cannot connect to server.', true); }
        finally { button.disabled = false; }
    });
    $('data-form').addEventListener('reset', () => {
        document.querySelectorAll('.option-btn').forEach(button => {
            button.classList.remove('selected'); button.setAttribute('aria-pressed', 'false');
        });
        // Reset dynamic hidden values after the browser's default form reset.
        setTimeout(() => { $('vehicle').value = ''; $('personnel').value = ''; }, 0);
    });
    $('close-modal-btn').addEventListener('click', () => $('success-modal').classList.add('hidden'));
    $('nav-report').addEventListener('click', () => {
        reportVisible = true; $('data-entry-container').classList.add('hidden');
        $('report-container').classList.remove('hidden'); refresh();
    });
    $('nav-entry').addEventListener('click', () => {
        reportVisible = false; $('data-entry-container').classList.remove('hidden');
        $('report-container').classList.add('hidden');
    });
    $('filter-btn').addEventListener('click', () => renderReport().catch(error => message(error.message, true)));
    $('clear-filter-btn').addEventListener('click', () => {
        $('filter-date').value = ''; $('filter-vehicle-no').value = '';
        renderReport().catch(error => message(error.message, true));
    });
    $('avail-btn').addEventListener('click', refresh);
    $('predict-btn').addEventListener('click', () => {
        predictionRequested = true; loadPrediction().catch(error => message(error.message, true));
    });
    $('nav-transport').addEventListener('click', () => {
        $('transport-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
        $('trip-distance').focus({preventScroll: true});
    });
    let tripRevision = 0;
    function clearRecommendation() {
        tripRevision++;
        $('transport-result').classList.add('hidden');
        $('transport-result').replaceChildren();
        $('transport-error').textContent = '';
    }
    function syncTransportFields() {
        const hasPublic = $('trip-public').value === 'yes';
        $('trip-walking').disabled = !hasPublic;
        $('trip-walking').required = hasPublic;
    }
    $('transport-form').addEventListener('input', clearRecommendation);
    $('transport-form').addEventListener('change', () => { syncTransportFields(); clearRecommendation(); });
    syncTransportFields();
    function textElement(tag, text) {
        const element = document.createElement(tag); element.textContent = text; return element;
    }
    $('transport-form').addEventListener('submit', async event => {
        event.preventDefault();
        const button = $('transport-submit');
        if (button.disabled) return;
        clearRecommendation();
        const revision = tripRevision;
        button.disabled = true; button.textContent = 'Calculating recommendation…';
        try {
            const result = await api('/transport/recommend', {
                vehicle: $('trip-vehicle').value,
                distanceKm: Number($('trip-distance').value),
                publicAvailable: $('trip-public').value,
                walkingMeters: $('trip-public').value === 'yes' ? Number($('trip-walking').value) : null,
                congestion: $('trip-congestion').value
            });
            if (revision !== tripRevision) return;
            const output = $('transport-result'); output.replaceChildren();
            output.append(textElement('h3', 'Prediction details'));
            const parking = result.parking;
            output.append(textElement('p', `${parking.vehicle}: ${parking.available} out of ${parking.capacity} parking slots free (${parking.occupied} occupied). Private threshold: ${parking.threshold} free slots.`));
            output.append(textElement('p', 'Decision is based on current parking slots, not forecast probability or a trained model.'));
            const reasons = document.createElement('ul');
            for (const reason of result.reasons) reasons.append(textElement('li', reason));
            output.append(reasons);
            for (const mode of ['public', 'private']) {
                if (result.constraints[mode].length) {
                    output.append(textElement('h4', `${mode === 'public' ? 'Public' : 'Private'} option limitations`));
                    const list = document.createElement('ul');
                    for (const reason of result.constraints[mode]) list.append(textElement('li', reason));
                    output.append(list);
                }
            }
            output.append(textElement('p', result.method), textElement('p', result.dataNote));
            const final = document.createElement('div'); final.className = `final-recommendation recommendation-${result.recommendation}`;
            final.append(textElement('h3', `Final recommendation: ${result.headline}`), textElement('p', result.explanation));
            output.append(final); output.classList.remove('hidden'); output.focus({preventScroll: true});
            final.scrollIntoView({behavior: 'smooth', block: 'center'});
        } catch (error) {
            if (revision === tripRevision) $('transport-error').textContent = error.message;
        } finally { button.disabled = false; button.textContent = 'Predict & Recommend Transport'; }
    });
    $('logout-btn').addEventListener('click', async () => {
        try { await api('/api/logout', {}); window.location.replace('login.html'); }
        catch (error) { message(error.message, true); }
    });
    await refresh();
    setInterval(async () => {
        if (document.hidden || refreshBusy) return;
        refreshBusy = true;
        try { await refresh(); } finally { refreshBusy = false; }
    }, 15000);
});
