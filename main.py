from flask import Flask, render_template, request, redirect, url_for, send_file, session
import uuid
from datetime import datetime, timedelta
from io import StringIO, BytesIO
import csv
import os
import pickle
import logging
from filelock import FileLock

app = Flask(__name__)
app.secret_key = 'dev-secret'

logging.basicConfig(level=logging.INFO)

ROOMS_FILE = 'rooms.pkl'
LOCK_FILE = f'{ROOMS_FILE}.lock'

rooms = {}


def load_rooms():
    """Load rooms data from disk."""
    global rooms
    try:
        lock = FileLock(LOCK_FILE)
        with lock:
            if os.path.exists(ROOMS_FILE):
                with open(ROOMS_FILE, 'rb') as f:
                    rooms = pickle.load(f)
                for r in rooms.values():
                    if not hasattr(r, 'current_picker'):
                        r.current_picker = None
    except (IOError, pickle.PickleError) as exc:
        logging.error("Failed to load rooms: %s", exc)
        rooms = {}


def save_rooms():
    """Persist rooms data to disk."""
    try:
        lock = FileLock(LOCK_FILE)
        with lock, open(ROOMS_FILE, 'wb') as f:
            pickle.dump(rooms, f)
    except (IOError, pickle.PickleError) as exc:
        logging.error("Failed to save rooms: %s", exc)


load_rooms()

class Room:
    def __init__(self, host, start_date, end_date):
        self.host = host
        self.start_date = start_date
        self.end_date = end_date
        self.available_dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
        self.participants = []
        self.picks = {}
        self.weekday_order = []
        self.weekend_order = []
        self.weekday_index = 0
        self.weekend_index = 0
        self.phase = 'weekday'
        self.history = []
        self.current_picker = None

    def add_participant(self, name):
        if name not in self.participants:
            self.participants.append(name)

    def remove_participant(self, name):
        if name in self.participants:
            self.participants.remove(name)
        if name in self.weekday_order:
            self.weekday_order.remove(name)
        if name in self.weekend_order:
            self.weekend_order.remove(name)
        if name in self.picks:
            date = self.picks.pop(name)
            if sum(1 for d in self.picks.values() if d == date) < 2 and date not in self.available_dates:
                self.available_dates.append(date)
                self.available_dates.sort()
        self.history = [n for n in self.history if n != name]
        if self.current_picker == name:
            self.current_picker = self.next_picker()

    def set_order(self, weekday_list, weekend_list):
        self.weekday_order = [n for n in weekday_list if n in self.participants]
        self.weekend_order = [n for n in weekend_list if n in self.participants]
        self.weekday_index = 0
        self.weekend_index = 0
        self.phase = 'weekday'
        self.history = []
        self.current_picker = None

    def next_picker(self):
        available = self.available_for_current_phase()
        if not available:
            if self.phase == 'weekday':
                self.phase = 'weekend'
                self.weekend_index = 0
                available = self.available_for_current_phase()
                if not available:
                    return None
            else:
                return None
        if self.phase == 'weekday':
            name = self.weekday_order[self.weekday_index]
            self.weekday_index = (self.weekday_index + 1) % len(self.weekday_order)
            return name
        name = self.weekend_order[self.weekend_index]
        self.weekend_index = (self.weekend_index + 1) % len(self.weekend_order)
        return name

    def register_pick(self, name, date):
        if name != self.current_picker:
            return False
        if sum(1 for d in self.picks.values() if d == date) >= 2:
            return False
        self.picks[name] = date
        self.history.append(name)
        if sum(1 for d in self.picks.values() if d == date) >= 2 and date in self.available_dates:
            self.available_dates.remove(date)
        self.current_picker = None
        return True

    def available_for_current_phase(self):
        if self.phase == 'weekday':
            return [d for d in self.available_dates if d.weekday() not in (4, 5)]
        return [d for d in self.available_dates if d.weekday() in (4, 5)]

    def undo_last(self):
        if not self.history:
            self.current_picker = None
            return
        name = self.history.pop()
        if name in self.picks:
            date = self.picks.pop(name)
            if sum(1 for d in self.picks.values() if d == date) < 2 and date not in self.available_dates:
                self.available_dates.append(date)
                self.available_dates.sort()
            if date.weekday() in (4, 5):
                self.phase = 'weekend'
                self.weekend_index = self.weekend_order.index(name)
            else:
                self.phase = 'weekday'
                self.weekday_index = self.weekday_order.index(name)
        self.current_picker = name

    def export_csv(self):
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Participant', 'Date'])
        for name, date in self.picks.items():
            writer.writerow([name, date])
        output.seek(0)
        return output

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/host', methods=['GET', 'POST'])
def host_login():
    if request.method == 'POST':
        session['host'] = request.form['name']
        return redirect(url_for('create_room'))
    return render_template('host_login.html')

@app.route('/create', methods=['GET', 'POST'])
def create_room():
    host = session.get('host')
    if not host:
        return redirect(url_for('host_login'))
    if request.method == 'POST':
        start = datetime.fromisoformat(request.form['start']).date()
        end = datetime.fromisoformat(request.form['end']).date()
        code = uuid.uuid4().hex[:6]
        rooms[code] = Room(host, start, end)
        save_rooms()
        return redirect(url_for('host_room', code=code))
    return render_template('create_room.html')

@app.route('/join', methods=['GET', 'POST'])
def join():
    if request.method == 'POST':
        code = request.form['code']
        name = request.form['name']
        room = rooms.get(code)
        if room:
            room.add_participant(name)
            save_rooms()
            session['participant'] = name
            return render_template('join_success.html', code=code, name=name)
    return render_template('join_room.html')

@app.route('/host/<code>')
def host_room(code):
    room = rooms.get(code)
    if not room:
        return redirect(url_for('index'))
    return render_template(
        'host_room.html',
        code=code,
        participants=room.participants,
        weekday_order=room.weekday_order,
        weekend_order=room.weekend_order,
    )

@app.route('/kick/<code>/<name>')
def kick(code, name):
    room = rooms.get(code)
    if room and session.get('host') == room.host:
        room.remove_participant(name)
        save_rooms()
    return redirect(url_for('host_room', code=code))

@app.route('/set_order/<code>', methods=['POST'])
def set_order(code):
    room = rooms.get(code)
    if room and session.get('host') == room.host:
        weekday_order = request.form.get('weekday_order', '')
        weekend_order = request.form.get('weekend_order', '')
        weekday_names = [n.strip() for n in weekday_order.split(',') if n.strip()]
        weekend_names = [n.strip() for n in weekend_order.split(',') if n.strip()]
        room.set_order(weekday_names, weekend_names)
        save_rooms()
    return redirect(url_for('host_room', code=code))

@app.route('/start/<code>')
def start(code):
    room = rooms.get(code)
    if not room or session.get('host') != room.host or (not room.weekday_order and not room.weekend_order):
        return redirect(url_for('host_room', code=code))
    name = room.next_picker()
    room.current_picker = name
    save_rooms()
    if name:
        return redirect(url_for('pick', code=code, name=name))
    return redirect(url_for('host_room', code=code))

@app.route('/pick/<code>/<name>', methods=['GET', 'POST'])
def pick(code, name):
    room = rooms.get(code)
    if not room:
        return redirect(url_for('index'))
    if session.get('participant') != name or room.current_picker != name:
        # redirect to the current picker if someone tries to skip the order
        current = room.current_picker
        if current:
            return redirect(url_for('pick', code=code, name=current))
        return redirect(url_for('host_room', code=code))
    if request.method == 'POST':
        date_str = request.form.get('date')
        if date_str:
            date_obj = datetime.fromisoformat(date_str).date()
            if date_obj in room.available_dates:
                if room.register_pick(name, date_obj):
                    next_name = room.next_picker()
                    room.current_picker = next_name
                    save_rooms()
                    if next_name:
                        return redirect(url_for('pick', code=code, name=next_name))
                    return redirect(url_for('host_room', code=code))
        return redirect(url_for('pick', code=code, name=name))
    picks_by_date = {}
    for participant, date in room.picks.items():
        picks_by_date.setdefault(date.isoformat(), []).append(participant)
    allowed_dates = [d.isoformat() for d in sorted(room.available_for_current_phase())]
    return render_template(
        'pick.html',
        name=name,
        start=room.start_date.isoformat(),
        end=room.end_date.isoformat(),
        picks=picks_by_date,
        allowed=allowed_dates,
    )

@app.route('/undo/<code>')
def undo(code):
    room = rooms.get(code)
    if room and session.get('host') == room.host:
        room.undo_last()
        save_rooms()
    return redirect(url_for('host_room', code=code))

@app.route('/export/<code>')
def export(code):
    room = rooms.get(code)
    if not room:
        return redirect(url_for('index'))
    csv_data = room.export_csv()
    data = BytesIO(csv_data.getvalue().encode())
    return send_file(data, mimetype='text/csv', as_attachment=True, download_name=f'{code}_results.csv')

if __name__ == '__main__':
    app.run(debug=True)
