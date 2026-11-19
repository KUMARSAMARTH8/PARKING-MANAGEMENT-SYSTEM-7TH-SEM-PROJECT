'use strict';
const form = document.getElementById('login-form');
const error = document.getElementById('error-message');
const status = document.getElementById('connection-status');
const submit = form.querySelector('button[type="submit"]');
const demo = document.getElementById('demo-login');
async function checkConnection() {
    status.textContent = 'Checking server connection…';
    try {
        await ParkingAPI.health(); status.textContent = 'Server connected. Ready to log in.'; return true;
    } catch (failure) {
        status.textContent = 'Server not connected'; error.textContent = failure.message; return false;
    }
}
form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true; demo.disabled = true; error.textContent = '';
    try {
        if (!await checkConnection()) return;
        await ParkingAPI.request('/api/login', {
            username: document.getElementById('username').value.trim(),
            password: document.getElementById('password').value
        });
        // Confirm the session cookie works before navigating, avoiding login loops.
        await ParkingAPI.request('/api/me');
        window.location.replace('index.html');
    } catch (failure) { error.textContent = failure.message; }
    finally { submit.disabled = false; demo.disabled = false; }
});
demo.addEventListener('click', () => {
    document.getElementById('username').value = 'Shubham';
    document.getElementById('password').value = 'shuchak';
    form.requestSubmit();
});
checkConnection();
