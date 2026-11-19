'use strict';
// Shared same-origin API client. Open the app through the Python server.
window.ParkingAPI = (() => {
    const startupMessage = 'Backend not connected. Extract the ZIP and run START_WINDOWS.bat, then use the browser page it opens. Do not use Live Server.';
    async function request(path, data) {
        if (window.location.protocol === 'file:') throw new Error(startupMessage);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(path, {
                method: data === undefined ? 'GET' : 'POST',
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                ...(data === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
            });
            if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error(startupMessage);
            const result = await response.json();
            if (!response.ok || result.success !== true) {
                const error = new Error(result.error || 'The request could not be completed.');
                error.status = response.status;
                throw error;
            }
            return result;
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('The server did not respond. Check that app.py is still running and retry.');
            if (error instanceof TypeError) throw new Error(startupMessage);
            throw error;
        } finally { clearTimeout(timer); }
    }
    async function health() {
        const result = await request('/api/health');
        if (result.service !== 'parking-management' || result.version !== 2) throw new Error('An older or different server is running. Close it and run the updated START_WINDOWS.bat.');
        return result;
    }
    return {request, health, startupMessage};
})();
