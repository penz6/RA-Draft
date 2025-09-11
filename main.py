from flask import Flask, render_template, request, redirect, url_for, send_file, session
import uuid
from datetime import datetime, timedelta
from io import StringIO, BytesIO
import csv

app = Flask(__name__)
app.secret_key = 'dev-secret'

rooms = {}

class Room:
    def __init__(self, host, start_date, end_date):
        self.host = host
        self.start_date = start_date
        self.end_date = end_date
        self.available_dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
        self.participants = []
        self.order = []
        self.picks = {}
        self.current = 0

    def add_participant(self, name):
        if name not in self.participants:
            self.participants.append(name)

    def remove_participant(self, name):
        if name in self.participants:
            self.participants.remove(name)
        if name in self.order:
            self.order.remove(name)
        if name in self.picks:
            date = self.picks.pop(name)
            self.available_dates.append(date)
            self.available_dates.sort()

    def set_order(self, order_list):
        self.order = [n for n in order_list if n in self.participants]

    def next_picker(self):
        if self.current < len(self.order):
            name = self.order[self.current]
            self.current += 1
            return name
        return None

    def undo_last(self):
        if self.current > 0:
            self.current -= 1
            name = self.order[self.current]
            if name in self.picks:
                self.available_dates.append(self.picks.pop(name))
                self.available_dates.sort()

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
        start = datetime.fromisoformat(request.form['start'])
        end = datetime.fromisoformat(request.form['end'])
        code = uuid.uuid4().hex[:6]
        rooms[code] = Room(host, start, end)
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
            return render_template('join_success.html', code=code, name=name)
    return render_template('join_room.html')

@app.route('/host/<code>')
def host_room(code):
    room = rooms.get(code)
    if not room:
        return redirect(url_for('index'))
    return render_template('host_room.html', code=code, participants=room.participants, order=room.order)

@app.route('/kick/<code>/<name>')
def kick(code, name):
    room = rooms.get(code)
    if room:
        room.remove_participant(name)
    return redirect(url_for('host_room', code=code))

@app.route('/set_order/<code>', methods=['POST'])
def set_order(code):
    room = rooms.get(code)
    if room:
        order = request.form['order']
        names = [n.strip() for n in order.split(',') if n.strip()]
        room.set_order(names)
    return redirect(url_for('host_room', code=code))

@app.route('/start/<code>')
def start(code):
    room = rooms.get(code)
    if not room or not room.order:
        return redirect(url_for('host_room', code=code))
    name = room.next_picker()
    if name:
        return redirect(url_for('pick', code=code, name=name))
    return redirect(url_for('host_room', code=code))

@app.route('/pick/<code>/<name>', methods=['GET', 'POST'])
def pick(code, name):
    room = rooms.get(code)
    if not room:
        return redirect(url_for('index'))
    if request.method == 'POST':
        date_str = request.form.get('date')
        if date_str:
            date_obj = datetime.fromisoformat(date_str).date()
            if date_obj in room.available_dates:
                room.available_dates.remove(date_obj)
                room.picks[name] = date_obj
        next_name = room.next_picker()
        if next_name:
            return redirect(url_for('pick', code=code, name=next_name))
        return redirect(url_for('host_room', code=code))
    dates = [d.isoformat() for d in sorted(room.available_dates)]
    return render_template('pick.html', name=name, dates=dates)

@app.route('/undo/<code>')
def undo(code):
    room = rooms.get(code)
    if room:
        room.undo_last()
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
