"""Local parking application. Python 3.10+, standard library only."""
import csv
import hmac
import io
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import threading
import webbrowser
import sys
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('PARKING_DB', ROOT / 'parking.db'))
CAPACITY = {'Bicycle': int(os.environ.get('BICYCLE_CAPACITY', '10')),
            'Motorcycle': int(os.environ.get('MOTORCYCLE_CAPACITY', '10')),
            'Car': int(os.environ.get('CAR_CAPACITY', '10'))}
if any(value < 1 for value in CAPACITY.values()):
    raise ValueError('Parking capacities must be positive integers.')
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))  # Campus: India
SESSIONS = {}
HORIZON = 30 * 60

class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status

def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY, vehicle TEXT NOT NULL, personnel TEXT NOT NULL,
            passengers INTEGER NOT NULL CHECK(passengers > 0), vehicle_no TEXT NOT NULL,
            remarks TEXT NOT NULL DEFAULT '', entered_at REAL NOT NULL,
            exited_at REAL, entry_date TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS active_vehicle ON visits(vehicle_no)
            WHERE exited_at IS NULL;
        CREATE INDEX IF NOT EXISTS vehicle_history ON visits(vehicle, entered_at);
        CREATE INDEX IF NOT EXISTS visit_date ON visits(entry_date);
        ''')

def public_visit(row):
    return {'id': row['id'], 'date': row['entry_date'], 'vehicle': row['vehicle'],
            'personnel': row['personnel'], 'passengers': row['passengers'],
            'vehicleNo': row['vehicle_no'], 'remarks': row['remarks'],
            'status': 'Parked' if row['exited_at'] is None else 'Exited'}

def park(data):
    vehicle, personnel = data.get('vehicle'), data.get('personnel')
    if vehicle not in CAPACITY or personnel not in ('Student', 'Faculty', 'Staff'):
        raise APIError('Select a valid vehicle and personnel type.')
    passengers = data.get('passengers')
    if isinstance(passengers, bool) or not re.fullmatch(r'[1-9]\d{0,2}', str(passengers)):
        raise APIError('Passengers must be a whole number from 1 to 999.')
    number = data.get('vehicleNo', '')
    remarks = data.get('remarks', '')
    if not isinstance(number, str) or not isinstance(remarks, str):
        raise APIError('Vehicle number and remarks must be text.')
    number = re.sub(r'[\s-]+', '', number).upper()
    if not re.fullmatch(r'[A-Z0-9]{2,20}', number):
        raise APIError('Use 2–20 letters or digits for the vehicle number or bicycle ID.')
    if len(remarks) > 500:
        raise APIError('Remarks must be 500 characters or fewer.')
    # One write transaction prevents duplicate or over-capacity concurrent entries.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM visits WHERE vehicle_no=? AND exited_at IS NULL', (number,)).fetchone():
            raise APIError('This vehicle is already parked. Mark its exit first.', 409)
        occupied = db.execute('SELECT COUNT(*) FROM visits WHERE vehicle=? AND exited_at IS NULL', (vehicle,)).fetchone()[0]
        if occupied >= CAPACITY[vehicle]:
            raise APIError(f'No {vehicle.lower()} spaces are available.', 409)
        now = time.time()  # Always server controlled; ignores client dates/times.
        date = datetime.fromtimestamp(now, LOCAL_TZ).date().isoformat()
        cur = db.execute('INSERT INTO visits(vehicle,personnel,passengers,vehicle_no,remarks,entered_at,entry_date) VALUES(?,?,?,?,?,?,?)',
                         (vehicle, personnel, int(passengers), number, remarks.strip(), now, date))
        row = db.execute('SELECT * FROM visits WHERE id=?', (cur.lastrowid,)).fetchone()
    return {'success': True, 'record': public_visit(row)}

def mark_exit(record_id):
    if type(record_id) is not int or record_id < 1:
        raise APIError('Invalid record ID.')
    with connect() as db:
        result = db.execute('UPDATE visits SET exited_at=? WHERE id=? AND exited_at IS NULL', (time.time(), record_id))
        if not result.rowcount:
            raise APIError('Record not found or vehicle already exited.', 409)
    return {'success': True}

def report(query):
    clauses, args = [], []
    date = query.get('date', [''])[0]
    number = query.get('vehicleNo', [''])[0]
    if date:
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise APIError('Invalid report date.')
        clauses.append('entry_date=?')
        args.append(date)
    if number:
        clauses.append('instr(vehicle_no, ?) > 0')
        args.append(re.sub(r'[\s-]+', '', number).upper())
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with connect() as db:
        rows = db.execute('SELECT * FROM visits' + where + ' ORDER BY id DESC', args).fetchall()
    return {'success': True, 'data': [public_visit(r) for r in rows], 'count': len(rows)}

def availability():
    with connect() as db:
        counts = dict(db.execute('SELECT vehicle,COUNT(*) FROM visits WHERE exited_at IS NULL GROUP BY vehicle').fetchall())
    slots = []
    for vehicle, capacity in CAPACITY.items():
        occupied = counts.get(vehicle, 0)
        slots.append({'vehicle': vehicle, 'capacity': capacity, 'occupied': occupied,
                      'available': max(0, capacity-occupied), 'percentFull': round(100*occupied/capacity, 1)})
    return {'success': True, 'slots': slots}

def predict_availability(now=None):
    """Empirical remaining-stay forecast plus recent mean arrival rate, not trained ML."""
    now = time.time() if now is None else now
    with connect() as db:
        rows = db.execute('SELECT * FROM visits WHERE entered_at>=? OR exited_at IS NULL', (now - 7*86400,)).fetchall()
    predictions = []
    for vehicle, capacity in CAPACITY.items():
        history = [r for r in rows if r['vehicle'] == vehicle]
        active = [r for r in history if r['exited_at'] is None]
        recent = [r for r in history if now-7*86400 <= r['entered_at'] <= now]
        durations = [r['exited_at']-r['entered_at'] for r in recent
                     if r['exited_at'] is not None and 0 < r['exited_at']-r['entered_at']]
        exposure = now-min((r['entered_at'] for r in recent), default=now)
        ready = len(durations) >= 3 and exposure >= 3600
        estimate = None
        if ready:
            departures = 0.0
            for row in active:
                age = max(0, now-row['entered_at'])
                survivors = [duration for duration in durations if duration > age]
                # If no comparable long stays exist, conservatively expect no exit.
                if survivors:
                    departures += sum(d <= age+HORIZON for d in survivors)/len(survivors)
            # The earliest arrival establishes observation start and isn't a later arrival.
            expected_arrivals = max(0, len(recent)-1) / exposure * HORIZON
            expected_occupied = min(capacity, max(0, len(active)-departures+expected_arrivals))
            estimate = max(0, min(capacity, math.floor(capacity-expected_occupied+0.5)))
        predictions.append({'vehicle': vehicle, 'capacity': capacity,
                            'currentAvailable': max(0, capacity-len(active)),
                            'predictedAvailable': estimate, 'completedSamples': len(durations),
                            'status': 'Estimate' if ready else 'Learning',
                            'message': 'Estimate based on recent arrivals and completed parking visits; not guaranteed.' if ready
                            else 'Learning from arrivals and departures. More history is needed.'})
    return {'success': True, 'predictions': predictions}

def recommend_transport(data):
    """Slot-first decision based on recorded occupancy, not an ML probability."""
    if not isinstance(data, dict):
        raise APIError('Provide valid trip details.')
    vehicle = data.get('vehicle')
    if not isinstance(vehicle, str) or vehicle not in CAPACITY:
        raise APIError('Select a valid vehicle type.')
    distance = data.get('distanceKm')
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or not 0.1 <= distance <= 300:
        raise APIError('Trip distance must be between 0.1 and 300 km.')
    public_option = data.get('publicAvailable')
    if public_option not in ('yes', 'no'):
        raise APIError('Select whether public transport is available.')
    walking = None
    if public_option == 'yes':
        walking = data.get('walkingMeters')
        if isinstance(walking, bool) or not isinstance(walking, (int, float)) or not math.isfinite(walking) or not 0 <= walking <= 10000:
            raise APIError('Walking distance must be between 0 and 10000 metres.')
    congestion = data.get('congestion')
    if congestion not in ('low', 'medium', 'high'):
        raise APIError('Select valid traffic congestion.')

    slot = next(item for item in availability()['slots'] if item['vehicle'] == vehicle)
    free, capacity = slot['available'], slot['capacity']
    # Empty/low parking is the dominant factor; congestion/distance are advisory only.
    # Threshold is proportional for customized small capacities, never exceeds 4 slots.
    comfortable = min(4, max(1, math.ceil(capacity * 0.4)))
    public_blocks = [] if public_option == 'yes' else ['Public transport is not available on this route.']
    private_blocks = [] if free > 0 else [f'No {vehicle.lower()} parking slots are currently free.']
    if public_blocks and private_blocks:
        recommendation, headline = 'alternative', 'Arrange another suitable transport option'
        explanation = 'The selected parking is full and public transport is unavailable. Consider another parking area or route.'
    elif private_blocks:
        recommendation, headline = 'public', 'Use public transport'
        explanation = f'All {capacity} {vehicle.lower()} parking slots are occupied.'
    elif public_blocks:
        recommendation, headline = 'private', 'Use private transport'
        explanation = f'Public transport is unavailable and {free} {vehicle.lower()} parking slots remain.'
    elif free < comfortable:
        recommendation, headline = 'public', 'Use public transport'
        explanation = f'Only {free} of {capacity} {vehicle.lower()} parking slots remain; available parking is low.'
    else:
        recommendation, headline = 'private', 'Use private transport'
        explanation = f'{free} of {capacity} {vehicle.lower()} parking slots remain, meeting the {comfortable}-slot availability threshold.'

    # Clearly labeled decision indicators, not trained-model confidence or probabilities.
    private_score = round(100 * free / capacity)
    public_score = 100 - private_score
    if private_blocks: private_score = 0
    if public_blocks: public_score = 0
    reasons = [f'{vehicle} parking: {free} free of {capacity} total slots ({slot["occupied"]} occupied).',
               f'The slot-based private recommendation threshold is {comfortable} available slot(s).']
    if public_option == 'yes':
        reasons.append(f'Public transport is available; distance to its stop: {walking:g} metres.')
    else:
        reasons.append('Public transport was reported as unavailable.')
    reasons.append(f'Trip distance: {distance:g} km; reported congestion: {congestion}. These are shown as trip context, not used to override slot availability.')
    return {'success': True, 'recommendation': recommendation, 'headline': headline,
            'explanation': explanation, 'scores': {'public': public_score, 'private': private_score},
            'feasible': {'public': not public_blocks, 'private': not private_blocks},
            'constraints': {'public': public_blocks, 'private': private_blocks},
            'reasons': reasons, 'parking': {'vehicle': vehicle, 'available': free, 'occupied': slot['occupied'],
                'capacity': capacity, 'threshold': comfortable, 'predictedAvailable': None,
                'forecastStatus': 'Not used', 'required': True},
            'method': 'Rule-based recommendation driven by CURRENT parking slots, not ML probability.',
            'dataNote': 'Current slots come from recorded vehicle entry/exit. The future forecast is displayed separately and does not override current availability.'}

class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, body, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200, headers=None):
        self.send_bytes(json.dumps(data).encode(), 'application/json; charset=utf-8', status, headers)

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['parking_session'].value if 'parking_session' in cookie else ''
        except Exception:
            token = ''
        value = SESSIONS.get(token)
        if not value or value[1] < time.time():
            SESSIONS.pop(token, None)
            raise APIError('Please log in to continue.', 401)
        return token, value[0]

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            path = url.path
            public = {'/': 'login.html', '/login.html': 'login.html', '/index.html': 'index.html',
                      '/style.css': 'style.css', '/script.js': 'script.js', '/login.css': 'login.css',
                      '/login.js': 'login.js', '/api.js': 'api.js', '/images.jpg': 'images.jpg'}
            if path in public:
                file = ROOT / public[path]
                return self.send_bytes(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            if path == '/api/health':
                return self.json({'success': True, 'service': 'parking-management', 'version': 2})
            if path == '/favicon.ico':
                return self.send_bytes(b'', 'image/x-icon', 204)
            _, user = self.session()
            if path == '/api/me':
                return self.json({'success': True, 'user': user})
            if path == '/report':
                return self.json(report(parse_qs(url.query)))
            if path == '/availability':
                return self.json(availability())
            if path == '/predict':
                return self.json(predict_availability())
            if path == '/excel':
                rows = report(parse_qs(url.query))['data']
                out = io.StringIO(newline='')
                writer = csv.writer(out)
                fields = ['date', 'vehicle', 'personnel', 'passengers', 'vehicleNo', 'status', 'remarks']
                writer.writerow(['Date', 'Vehicle Type', 'Personnel', 'Passengers', 'Vehicle No.', 'Status', 'Remarks'])
                for row in rows:
                    values = [str(row[k]) for k in fields]
                    writer.writerow(["'"+v if v.lstrip().startswith(('=', '+', '-', '@')) else v for v in values])
                return self.send_bytes(('\ufeff'+out.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8',
                                       headers={'Content-Disposition': 'attachment; filename="parking_report.csv"'})
            raise APIError('Not found.', 404)
        except APIError as exc:
            self.json({'success': False, 'error': exc.message}, exc.status)
        except Exception:
            self.json({'success': False, 'error': 'Server error. Check the application terminal.'}, 500)
            import traceback
            traceback.print_exc()

    def do_POST(self):
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise APIError('JSON is required.', 415)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                raise APIError('Invalid request origin.', 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise APIError('Invalid request size.')
            if length <= 0 or length > 10000:
                raise APIError('Invalid request size.')
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise APIError('Invalid JSON.')
            if not isinstance(data, dict):
                raise APIError('Expected a JSON object.')
            path = urlsplit(self.path).path
            if path == '/api/login':
                username, password = data.get('username'), data.get('password')
                if not isinstance(username, str) or not isinstance(password, str):
                    raise APIError('Enter username and password.')
                if not (hmac.compare_digest(username.encode(), os.environ.get('PARKING_USER', 'Shubham').encode()) and
                        hmac.compare_digest(password.encode(), os.environ.get('PARKING_PASSWORD', 'shuchak').encode())):
                    raise APIError('Invalid username or password.', 401)
                for old, (_, expiry) in list(SESSIONS.items()):
                    if expiry < time.time():
                        SESSIONS.pop(old, None)
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = (username, time.time()+12*3600)
                return self.json({'success': True}, headers={'Set-Cookie': f'parking_session={token}; HttpOnly; SameSite=Strict; Path=/'})
            token, _ = self.session()
            if path == '/api/logout':
                SESSIONS.pop(token, None)
                return self.json({'success': True}, headers={'Set-Cookie': 'parking_session=; Max-Age=0; Path=/'})
            if path == '/transport/recommend':
                return self.json(recommend_transport(data))
            if path == '/submit':
                return self.json(park(data), 201)
            if path == '/exit':
                return self.json(mark_exit(data.get('id')))
            raise APIError('Not found.', 404)
        except APIError as exc:
            self.json({'success': False, 'error': exc.message}, exc.status)
        except Exception:
            self.json({'success': False, 'error': 'Server error. Check the application terminal.'}, 500)
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    try:
        init_db()
        port = int(os.environ.get('PORT', '5000'))
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        url = f'http://127.0.0.1:{port}'
        print(f'Parking Management System: {url}', flush=True)
        print('Keep this terminal open. Press Ctrl+C to stop.', flush=True)
        if '--open' in sys.argv:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except KeyboardInterrupt:
        print('\nStopped.')
    except OSError as error:
        print(f'Could not start server: {error}', flush=True)
        print('Close any older parking server, then run START_WINDOWS.bat again. If another app uses the port, set PORT to a different number.', flush=True)
        sys.exit(1)
