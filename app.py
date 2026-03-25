# TOP OF app.py
from gevent import monkey
monkey.patch_all()
import os
from flask import Flask, render_template... # other imports follow
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps
import zipfile, io, atexit

from utils import generate_visual_pdf, calculate_kra_grade, calculate_efficiency_score

app = Flask(__name__)
app.secret_key = "ip_pharma_ultra_secure_v4"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pharma_v5.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

class Employee(db.Model):
    __tablename__ = 'employees'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    staff_type    = db.Column(db.String(20), default='picker')
    role          = db.Column(db.String(100), default='Operations Specialist')
    is_admin      = db.Column(db.Boolean, default=False)
    entries       = db.relationship('KPIEntry', backref='owner', lazy=True)
    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class KPIEntry(db.Model):
    __tablename__  = 'kpi_entries'
    id             = db.Column(db.Integer, primary_key=True)
    emp_id         = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    bills          = db.Column(db.Integer, default=0)
    picked         = db.Column(db.Integer, default=0)
    missed         = db.Column(db.Integer, default=0)
    boxes          = db.Column(db.Integer, default=0)
    sweep          = db.Column(db.Float,   default=0.0)
    checked        = db.Column(db.Integer, default=0)
    errors_found   = db.Column(db.Integer, default=0)
    check_time     = db.Column(db.Float,   default=0.0)
    entry_date     = db.Column(db.Date, nullable=False, index=True)
    report_sent    = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint('emp_id', 'entry_date', name='_emp_date_uc'),)

    @property
    def accuracy(self):
        t = self.picked + self.missed
        return round(self.picked / t * 100, 1) if t > 0 else 0.0
    @property
    def pick_speed(self):
        hrs = self.sweep if self.sweep > 0 else 1
        return round((self.picked + self.missed) / hrs, 1)
    @property
    def check_rate(self):
        if self.checked == 0: return 0.0
        return round(self.errors_found / self.checked * 100, 1)

def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('is_admin'):
            flash("Admin access required.", "danger")
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return d

def build_analytics(entries, staff_type='picker'):
    if not entries: return None
    tp  = sum(e.picked       for e in entries)
    tm  = sum(e.missed       for e in entries)
    ti  = tp + tm
    tb  = sum(e.bills        for e in entries)
    tbx = sum(e.boxes        for e in entries)
    ts  = round(sum(e.sweep  for e in entries), 2)
    tck = sum(e.checked      for e in entries)
    ter = sum(e.errors_found for e in entries)
    tct = round(sum(e.check_time for e in entries), 2)

    pick_acc   = round(tp / ti * 100, 1)      if ti  > 0 else 0.0
    pick_speed = round(ti / ts, 1)             if ts  > 0 else 0.0
    check_acc  = round(ter / tck * 100, 1)     if tck > 0 else 0.0
    ck_speed   = round(tck / tct, 1)           if tct > 0 else 0.0

    eff_score  = calculate_efficiency_score(staff_type, pick_acc, pick_speed, check_acc, tct, tck)
    grade, feedback = calculate_kra_grade(tp, tm, tbx, staff_type)

    # Potential
    if staff_type == 'picker':
        bench_speed = 200.0
        bench_acc   = 98.0
        potential_items = round(bench_speed * ts)
        potential_eff   = round(min(100, eff_score + max(0, (bench_acc - pick_acc) * 0.5 + (bench_speed - pick_speed) * 0.1)), 1)
    else:
        bench_speed = 150.0
        bench_acc   = 100.0
        potential_items = round(bench_speed * tct) if tct > 0 else tck
        potential_eff   = round(min(100, eff_score + max(0, (bench_acc - check_acc) * 0.5)), 1)

    return dict(
        tp=tp, tm=tm, ti=ti, tb=tb, tbx=tbx, ts=ts,
        tck=tck, ter=ter, tct=tct,
        pick_acc=pick_acc, pick_speed=pick_speed,
        check_acc=check_acc, ck_speed=ck_speed,
        eff_score=eff_score, grade=grade, feedback=feedback,
        potential_items=potential_items, potential_eff=potential_eff,
        days=len(entries)
    )

def period_entries(emp_id, period):
    today = date.today()
    start = {
        'day':   today,
        'week':  today - timedelta(days=6),
        'month': today - timedelta(days=29)
    }.get(period, today)
    return KPIEntry.query.filter(
        KPIEntry.emp_id == emp_id,
        KPIEntry.entry_date >= start,
        KPIEntry.entry_date <= today
    ).order_by(KPIEntry.entry_date.desc()).all()

def auto_report_job():
    with app.app_context():
        target  = date.today() - timedelta(days=2)
        pending = KPIEntry.query.filter_by(entry_date=target, report_sent=False).all()
        for e in pending:
            emp = Employee.query.get(e.emp_id)
            if emp: print(f"[48HR] {emp.name} — {target}")
            e.report_sent = True
        db.session.commit()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(auto_report_job, 'interval', hours=24, id='auto_report')
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard' if session.get('is_admin') else 'dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        user  = Employee.query.filter_by(email=email).first()
        if user and user.check_password(request.form.get('password', '')):
            session.update({'user_id': user.id, 'user_name': user.name,
                            'staff_type': user.staff_type, 'is_admin': user.is_admin})
            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))
        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    emp_id     = session['user_id']
    staff_type = session.get('staff_type', 'picker')
    today      = date.today()
    today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()

    if request.method == 'POST' and not today_entry:
        try:
            picked = int(request.form.get('picked', 0))
            missed = int(request.form.get('missed', 0))
            ne = KPIEntry(
                emp_id       = emp_id,
                bills        = int(request.form.get('bills', 0)),
                picked       = picked, missed=missed,
                boxes        = int(request.form.get('boxes', 0)),
                sweep        = round(float(request.form.get('sweep_mins', 0)) / 60, 3),
                checked      = int(request.form.get('checked', 0)),
                errors_found = int(request.form.get('errors_found', 0)),
                check_time   = round(float(request.form.get('check_mins', 0)) / 60, 3),
                entry_date   = today
            )
            db.session.add(ne)
            db.session.commit()
            today_entry = ne
            total = picked + missed
            eff   = round(picked / total * 100, 1) if total > 0 else 0.0
            if eff < 85:
                socketio.emit('admin_alert', {'name': session['user_name'], 'eff': eff, 'type': staff_type})
            flash("Today's metrics recorded successfully.", "success")
        except Exception:
            db.session.rollback()
            flash("Error saving. Please try again.", "warning")

    all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
    d_stats = build_analytics(period_entries(emp_id, 'day'),   staff_type)
    w_stats = build_analytics(period_entries(emp_id, 'week'),  staff_type)
    m_stats = build_analytics(period_entries(emp_id, 'month'), staff_type)
    a_stats = build_analytics(all_entries, staff_type)

    peers   = Employee.query.filter_by(staff_type=staff_type, is_admin=False).all()
    lb      = []
    for p in peers:
        pe = KPIEntry.query.filter_by(emp_id=p.id).all()
        if not pe: continue
        ps = build_analytics(pe, p.staff_type)
        lb.append({'name': p.name, 'score': ps['eff_score'], 'grade': ps['grade'],
                   'pick_acc': ps['pick_acc'], 'pick_speed': ps['pick_speed'],
                   'check_acc': ps['check_acc']})
    lb.sort(key=lambda x: x['score'], reverse=True)
    my_rank = next((i+1 for i,x in enumerate(lb) if x['name']==session['user_name']), '-')

    return render_template('dashboard.html',
        user_name=session['user_name'], staff_type=staff_type,
        today=today, today_entry=today_entry,
        d_stats=d_stats, w_stats=w_stats, m_stats=m_stats, a_stats=a_stats,
        leaderboard=lb, my_rank=my_rank,
        recent=all_entries[:10]
    )

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    employees = Employee.query.filter_by(is_admin=False).all()
    today = date.today()
    rows = []
    for emp in employees:
        ents = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.desc()).all()
        d_ent = [e for e in ents if e.entry_date == today]
        w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
        m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
        rows.append(dict(
            emp=emp,
            stats  =build_analytics(ents,  emp.staff_type),
            d_stats=build_analytics(d_ent, emp.staff_type),
            w_stats=build_analytics(w_ent, emp.staff_type),
            m_stats=build_analytics(m_ent, emp.staff_type),
            count=len(ents)
        ))
    pickers  = sorted([r for r in rows if r['emp'].staff_type=='picker'  and r['stats']],
                      key=lambda x: x['stats']['eff_score'], reverse=True)
    checkers = sorted([r for r in rows if r['emp'].staff_type=='checker' and r['stats']],
                      key=lambda x: x['stats']['eff_score'], reverse=True)
    return render_template('admin.html', rows=rows, pickers=pickers, checkers=checkers)

@app.route('/download/<int:emp_id>')
@login_required
@admin_required
def download_report(emp_id):
    emp   = Employee.query.get_or_404(emp_id)
    ents  = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.asc()).all()
    if not ents:
        flash("No data for this employee.", "warning")
        return redirect(url_for('admin_dashboard'))
    today = date.today()
    d_ent = [e for e in ents if e.entry_date == today]
    w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
    m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
    payload = dict(
        all_entries=ents,
        all_stats  =build_analytics(ents,  emp.staff_type),
        day_stats  =build_analytics(d_ent, emp.staff_type),
        week_stats =build_analytics(w_ent, emp.staff_type),
        month_stats=build_analytics(m_ent, emp.staff_type),
        staff_type =emp.staff_type,
    )
    buf = generate_visual_pdf(emp.name, payload)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f"KRA_{emp.name.replace(' ','_')}.pdf")

@app.route('/bulk_zip')
@login_required
@admin_required
def bulk_zip():
    employees = Employee.query.filter_by(is_admin=False).all()
    zbuf      = io.BytesIO()
    today     = date.today()
    with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for emp in employees:
            ents = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.asc()).all()
            if not ents: continue
            d_ent = [e for e in ents if e.entry_date == today]
            w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
            m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
            payload = dict(all_entries=ents,
                all_stats=build_analytics(ents,emp.staff_type),
                day_stats=build_analytics(d_ent,emp.staff_type),
                week_stats=build_analytics(w_ent,emp.staff_type),
                month_stats=build_analytics(m_ent,emp.staff_type),
                staff_type=emp.staff_type)
            pdf = generate_visual_pdf(emp.name, payload)
            zf.writestr(f"KRA_{emp.name.replace(' ','_')}.pdf", pdf.read())
    zbuf.seek(0)
    return send_file(zbuf, mimetype='application/zip', as_attachment=True,
                     download_name='All_KRA_Reports.zip')

@app.route('/health')
def health(): return {"status": "Pharma IP v4"}, 200

def seed_db():
    def make(name, email, pw, stype='picker', admin=False):
        user = Employee.query.filter_by(email=email).first()
        if user:
            return user
        e = Employee(name=name, email=email, staff_type=stype, is_admin=admin)
        e.set_password(pw)
        db.session.add(e)
        db.session.commit()
        return e

    # Create users
    make("System Admin", "admin@pharmaip.com", "admin123", admin=True)
    make("Rahul Sharma", "rahul@pharmaip.com", "test1234", stype='picker')
    make("Priya Patel", "priya@pharmaip.com", "test1234", stype='checker')
    
    print("Database Seeded Successfully.")

if __name__ == '__main__':
    with app.app_context():
        # 1. This creates the new v5 database file (fixing the 500 error)
        db.create_all()  
        
        # 2. This adds your users (Admin, Rahul, Priya)
        seed_db()        
    
    # 3. Dynamic Port: Render uses a random port, Local uses 5000
    port = int(os.environ.get("PORT", 5000))
    
    # 4. Start the server with host 0.0.0.0 for Render compatibility
    socketio.run(app, debug=True, host='0.0.0.0', port=port)