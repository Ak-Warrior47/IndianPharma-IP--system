import os, logging, zipfile, io, atexit
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ip_pharma_ultra_secure_v5_change_in_prod')

# Render uses PostgreSQL via DATABASE_URL env var, fallback to SQLite locally
db_url = os.environ.get('DATABASE_URL', 'sqlite:///pharma_v5.db')
# Render sets postgres://, SQLAlchemy needs postgresql://
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 300}

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# ══════════════════════════════════════════
#  MODELS
# ══════════════════════════════════════════
class Employee(db.Model):
    __tablename__ = 'employees'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    staff_type    = db.Column(db.String(20), default='picker')   # 'picker' | 'checker'
    role          = db.Column(db.String(100), default='Operations Specialist')
    is_admin      = db.Column(db.Boolean, default=False)
    entries       = db.relationship('KPIEntry', backref='owner', lazy='select', cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class KPIEntry(db.Model):
    __tablename__  = 'kpi_entries'
    id             = db.Column(db.Integer, primary_key=True)
    emp_id         = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
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
        hrs = self.sweep if self.sweep and self.sweep > 0 else 1
        return round((self.picked + self.missed) / hrs, 1)

    @property
    def check_rate(self):
        return round(self.errors_found / self.checked * 100, 1) if self.checked > 0 else 0.0


# ══════════════════════════════════════════
#  DECORATORS
# ══════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user_id' not in session:
            flash('Please sign in to continue.', 'warning')
            return redirect(url_for('login'))
        return f(*a, **kw)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get('is_admin'):
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return decorated


# ══════════════════════════════════════════
#  ANALYTICS HELPERS
# ══════════════════════════════════════════
def safe_div(a, b, default=0.0):
    try:
        return a / b if b and b != 0 else default
    except Exception:
        return default


def build_analytics(entries, staff_type='picker'):
    """Build analytics dict — never raises, always returns None or full dict."""
    try:
        if not entries:
            return None
        tp  = sum(int(e.picked or 0)       for e in entries)
        tm  = sum(int(e.missed or 0)       for e in entries)
        ti  = tp + tm
        tb  = sum(int(e.bills or 0)        for e in entries)
        tbx = sum(int(e.boxes or 0)        for e in entries)
        ts  = round(sum(float(e.sweep or 0)      for e in entries), 2)
        tck = sum(int(e.checked or 0)      for e in entries)
        ter = sum(int(e.errors_found or 0) for e in entries)
        tct = round(sum(float(e.check_time or 0) for e in entries), 2)

        pick_acc   = round(safe_div(tp, ti) * 100, 1)
        pick_speed = round(safe_div(ti, ts), 1)
        check_acc  = round(safe_div(ter, tck) * 100, 1)
        ck_speed   = round(safe_div(tck, tct), 1)

        # Efficiency score
        if staff_type == 'checker':
            eff_score = round(min(100, (check_acc / 100 * 70) + (min(ck_speed / 150, 1) * 30)), 1)
        else:
            eff_score = round(min(100, (pick_acc / 100 * 60) + (min(safe_div(pick_speed, 200), 1) * 40)), 1)

        # KRA Grade
        acc_for_grade = pick_acc
        if staff_type == 'picker':
            if acc_for_grade >= 98 and tbx >= 15:  grade, feedback = 'ELITE',        'Exceptional pick accuracy & CS handling. Gold Standard.'
            elif acc_for_grade >= 95:               grade, feedback = 'PROFICIENT',   'Meets standard pharma pick accuracy requirements.'
            elif acc_for_grade >= 88:               grade, feedback = 'SATISFACTORY', 'Acceptable. Focus on reducing missed picks.'
            else:                                   grade, feedback = 'RE-TRAINING',  'Pick accuracy below safety threshold. Intervention required.'
        else:
            if check_acc >= 97 and tbx >= 10:  grade, feedback = 'ELITE',        'Exceptional verification accuracy. Zero-error standard met.'
            elif check_acc >= 94:              grade, feedback = 'PROFICIENT',   'Good check accuracy. Minor improvements remain.'
            elif check_acc >= 87:              grade, feedback = 'SATISFACTORY', 'Acceptable check rate. Increase error detection.'
            else:                              grade, feedback = 'RE-TRAINING',  'Verification accuracy below threshold. Re-training needed.'

        # Potential
        if staff_type == 'picker':
            potential_items = int(200.0 * ts)
            potential_eff   = round(min(100, eff_score + max(0, (98 - pick_acc) * 0.5 + (200 - pick_speed) * 0.1)), 1)
        else:
            potential_items = int(150.0 * tct) if tct > 0 else tck
            potential_eff   = round(min(100, eff_score + max(0, (100 - check_acc) * 0.5)), 1)

        return dict(
            tp=tp, tm=tm, ti=ti, tb=tb, tbx=tbx, ts=ts,
            tck=tck, ter=ter, tct=tct,
            pick_acc=pick_acc, pick_speed=pick_speed,
            check_acc=check_acc, ck_speed=ck_speed,
            eff_score=eff_score, grade=grade, feedback=feedback,
            potential_items=potential_items, potential_eff=potential_eff,
            days=len(entries)
        )
    except Exception as e:
        logger.error(f'build_analytics error: {e}')
        return None


def get_period_entries(emp_id, period):
    today = date.today()
    starts = {'day': today, 'week': today - timedelta(days=6), 'month': today - timedelta(days=29)}
    start  = starts.get(period, today)
    try:
        return KPIEntry.query.filter(
            KPIEntry.emp_id == emp_id,
            KPIEntry.entry_date >= start,
            KPIEntry.entry_date <= today
        ).order_by(KPIEntry.entry_date.desc()).all()
    except Exception as e:
        logger.error(f'get_period_entries error: {e}')
        return []


# ══════════════════════════════════════════
#  48-HR AUTOMATION  (only 1 worker on Render)
# ══════════════════════════════════════════
def start_scheduler():
    def job():
        with app.app_context():
            try:
                target  = date.today() - timedelta(days=2)
                pending = KPIEntry.query.filter_by(entry_date=target, report_sent=False).all()
                for e in pending:
                    emp = db.session.get(Employee, e.emp_id)
                    if emp:
                        logger.info(f'[48HR] Processing KRA for {emp.name} — {target}')
                    e.report_sent = True
                db.session.commit()
            except Exception as err:
                logger.error(f'Scheduler job error: {err}')

    try:
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(job, 'interval', hours=24, id='auto_report', replace_existing=True)
        sched.start()
        atexit.register(lambda: sched.shutdown(wait=False))
        logger.info('Scheduler started')
    except Exception as e:
        logger.warning(f'Scheduler could not start: {e}')


# ══════════════════════════════════════════
#  DB INIT  (called at app startup, not just __main__)
# ══════════════════════════════════════════
def init_db():
    with app.app_context():
        try:
            db.create_all()

            def make(name, email, pw, stype='picker', admin=False):
                existing = Employee.query.filter_by(email=email).first()
                if existing:
                    return existing
                emp = Employee(
                    name=name, email=email, staff_type=stype, is_admin=admin,
                    role='Admin' if admin else f'Operations {stype.title()}'
                )
                emp.set_password(pw)
                db.session.add(emp)
                db.session.flush()
                return emp

            make('System Admin',  'admin@pharmaip.com',  'admin123', admin=True)

            p1 = make('Rahul Sharma', 'rahul@pharmaip.com', 'test1234', stype='picker')
            if p1 and not KPIEntry.query.filter_by(emp_id=p1.id).first():
                for d_ago, b, pk, ms, bx, sw in [
                    (6,40,195,5,10,1.5),(5,42,210,2,12,1.4),(4,38,188,8,9,1.6),
                    (3,45,220,1,14,1.3),(2,41,200,4,11,1.5),(1,44,215,3,13,1.4),(0,46,225,2,15,1.3)]:
                    db.session.add(KPIEntry(
                        emp_id=p1.id, bills=b, picked=pk, missed=ms, boxes=bx, sweep=sw,
                        entry_date=date.today() - timedelta(days=d_ago)
                    ))

            c1 = make('Priya Patel', 'priya@pharmaip.com', 'test1234', stype='checker')
            if c1 and not KPIEntry.query.filter_by(emp_id=c1.id).first():
                for d_ago, b, pk, ms, bx, sw, ck, er, cm in [
                    (6,30,160,18,8,2.0,178,14,90),(5,28,172,12,7,1.8,184,10,85),
                    (4,33,190,6,10,1.6,196,6,80),(3,29,168,14,6,1.9,182,12,88),
                    (2,35,195,9,11,1.5,204,8,82),(1,31,180,10,9,1.7,190,9,86),
                    (0,36,200,7,12,1.4,207,7,78)]:
                    db.session.add(KPIEntry(
                        emp_id=c1.id, bills=b, picked=pk, missed=ms, boxes=bx, sweep=sw,
                        checked=ck, errors_found=er, check_time=round(cm/60, 3),
                        entry_date=date.today() - timedelta(days=d_ago)
                    ))

            db.session.commit()
            logger.info('DB seeded successfully')
        except Exception as e:
            logger.error(f'DB init error: {e}')
            db.session.rollback()


# Run init at import time (works for both gunicorn and python app.py)
init_db()
start_scheduler()


# ══════════════════════════════════════════
#  ROUTES — AUTH
# ══════════════════════════════════════════
@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard' if session.get('is_admin') else 'dashboard'))

    if request.method == 'POST':
        try:
            email      = request.form.get('email', '').lower().strip()
            password   = request.form.get('password', '')
            role_choice = request.form.get('staff_type', '').strip()  # picker | checker | '' for admin

            user = Employee.query.filter_by(email=email).first()

            if not user:
                flash('No account found with that email.', 'danger')
                return render_template('login.html')

            if not user.check_password(password):
                flash('Incorrect password. Please try again.', 'danger')
                return render_template('login.html')

            # Update staff_type if user selected a role on login page (non-admins only)
            if not user.is_admin and role_choice in ('picker', 'checker'):
                user.staff_type = role_choice
                db.session.commit()

            session.permanent = True
            session['user_id']    = user.id
            session['user_name']  = user.name
            session['staff_type'] = user.staff_type
            session['is_admin']   = user.is_admin

            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))

        except Exception as e:
            logger.error(f'Login error: {e}')
            flash('System error during login. Please try again.', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ══════════════════════════════════════════
#  ROUTES — STAFF DASHBOARD
# ══════════════════════════════════════════
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    try:
        emp_id     = session['user_id']
        staff_type = session.get('staff_type', 'picker')
        today      = date.today()

        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()

        if request.method == 'POST' and not today_entry:
            try:
                picked     = max(0, int(request.form.get('picked', 0) or 0))
                missed     = max(0, int(request.form.get('missed', 0) or 0))
                sweep_mins = max(0, float(request.form.get('sweep_mins', 0) or 0))
                check_mins = max(0, float(request.form.get('check_mins', 0) or 0))

                ne = KPIEntry(
                    emp_id       = emp_id,
                    bills        = max(0, int(request.form.get('bills', 0) or 0)),
                    picked       = picked,
                    missed       = missed,
                    boxes        = max(0, int(request.form.get('boxes', 0) or 0)),
                    sweep        = round(sweep_mins / 60, 3),
                    checked      = max(0, int(request.form.get('checked', 0) or 0)),
                    errors_found = max(0, int(request.form.get('errors_found', 0) or 0)),
                    check_time   = round(check_mins / 60, 3),
                    entry_date   = today
                )
                db.session.add(ne)
                db.session.commit()
                today_entry = ne

                total = picked + missed
                eff   = round(picked / total * 100, 1) if total > 0 else 0.0
                if eff < 85:
                    try:
                        socketio.emit('admin_alert', {
                            'name': session['user_name'], 'eff': eff, 'type': staff_type
                        })
                    except Exception:
                        pass  # non-fatal if socket fails
                flash("Today's metrics recorded successfully.", 'success')

            except Exception as e:
                db.session.rollback()
                logger.error(f'Dashboard POST error: {e}')
                flash('Could not save entry. Please check your inputs and try again.', 'danger')

        # Build analytics safely
        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        d_stats = build_analytics(get_period_entries(emp_id, 'day'),   staff_type)
        w_stats = build_analytics(get_period_entries(emp_id, 'week'),  staff_type)
        m_stats = build_analytics(get_period_entries(emp_id, 'month'), staff_type)
        a_stats = build_analytics(all_entries, staff_type)

        # Leaderboard
        peers = Employee.query.filter_by(staff_type=staff_type, is_admin=False).all()
        lb    = []
        for p in peers:
            pe = KPIEntry.query.filter_by(emp_id=p.id).all()
            if not pe:
                continue
            ps = build_analytics(pe, p.staff_type)
            if ps:
                lb.append({
                    'name': p.name, 'score': ps['eff_score'],
                    'grade': ps['grade'], 'pick_acc': ps['pick_acc'],
                    'pick_speed': ps['pick_speed'], 'check_acc': ps['check_acc']
                })
        lb.sort(key=lambda x: x['score'], reverse=True)
        my_rank = next((i+1 for i, x in enumerate(lb) if x['name'] == session['user_name']), '-')

        return render_template('dashboard.html',
            user_name=session['user_name'], staff_type=staff_type,
            today=today, today_entry=today_entry,
            d_stats=d_stats, w_stats=w_stats, m_stats=m_stats, a_stats=a_stats,
            leaderboard=lb, my_rank=my_rank,
            recent=all_entries[:10]
        )

    except Exception as e:
        logger.error(f'Dashboard error: {e}')
        flash('An error occurred loading your dashboard. Please try again.', 'danger')
        return redirect(url_for('login'))


# ══════════════════════════════════════════
#  ROUTES — ADMIN
# ══════════════════════════════════════════
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    try:
        employees = Employee.query.filter_by(is_admin=False).all()
        today     = date.today()
        rows      = []

        for emp in employees:
            try:
                ents  = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.desc()).all()
                d_ent = [e for e in ents if e.entry_date == today]
                w_ent = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
                m_ent = [e for e in ents if e.entry_date >= today - timedelta(days=29)]
                rows.append(dict(
                    emp     = emp,
                    stats   = build_analytics(ents,  emp.staff_type),
                    d_stats = build_analytics(d_ent, emp.staff_type),
                    w_stats = build_analytics(w_ent, emp.staff_type),
                    m_stats = build_analytics(m_ent, emp.staff_type),
                    count   = len(ents)
                ))
            except Exception as ex:
                logger.error(f'Admin row error for {emp.name}: {ex}')

        pickers  = sorted([r for r in rows if r['emp'].staff_type == 'picker'  and r['stats']],
                          key=lambda x: x['stats']['eff_score'], reverse=True)
        checkers = sorted([r for r in rows if r['emp'].staff_type == 'checker' and r['stats']],
                          key=lambda x: x['stats']['eff_score'], reverse=True)

        return render_template('admin.html', rows=rows, pickers=pickers, checkers=checkers)

    except Exception as e:
        logger.error(f'Admin dashboard error: {e}')
        flash('Error loading admin dashboard.', 'danger')
        return redirect(url_for('login'))


@app.route('/download/<int:emp_id>')
@login_required
@admin_required
def download_report(emp_id):
    try:
        from utils import generate_visual_pdf
        emp   = db.session.get(Employee, emp_id)
        if not emp:
            flash('Employee not found.', 'danger')
            return redirect(url_for('admin_dashboard'))

        ents  = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.asc()).all()
        if not ents:
            flash('No data for this employee.', 'warning')
            return redirect(url_for('admin_dashboard'))

        today = date.today()
        d_ent = [e for e in ents if e.entry_date == today]
        w_ent = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
        m_ent = [e for e in ents if e.entry_date >= today - timedelta(days=29)]

        payload = dict(
            all_entries = ents,
            all_stats   = build_analytics(ents,  emp.staff_type),
            day_stats   = build_analytics(d_ent, emp.staff_type),
            week_stats  = build_analytics(w_ent, emp.staff_type),
            month_stats = build_analytics(m_ent, emp.staff_type),
            staff_type  = emp.staff_type,
        )
        buf = generate_visual_pdf(emp.name, payload)
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f"KRA_{emp.name.replace(' ','_')}.pdf")
    except Exception as e:
        logger.error(f'Download report error: {e}')
        flash('Could not generate report. Please try again.', 'danger')
        return redirect(url_for('admin_dashboard'))


@app.route('/bulk_zip')
@login_required
@admin_required
def bulk_zip():
    try:
        from utils import generate_visual_pdf
        employees = Employee.query.filter_by(is_admin=False).all()
        zbuf      = io.BytesIO()
        today     = date.today()

        with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for emp in employees:
                try:
                    ents = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.asc()).all()
                    if not ents:
                        continue
                    d_ent = [e for e in ents if e.entry_date == today]
                    w_ent = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
                    m_ent = [e for e in ents if e.entry_date >= today - timedelta(days=29)]
                    payload = dict(
                        all_entries = ents,
                        all_stats   = build_analytics(ents,  emp.staff_type),
                        day_stats   = build_analytics(d_ent, emp.staff_type),
                        week_stats  = build_analytics(w_ent, emp.staff_type),
                        month_stats = build_analytics(m_ent, emp.staff_type),
                        staff_type  = emp.staff_type,
                    )
                    pdf = generate_visual_pdf(emp.name, payload)
                    zf.writestr(f"KRA_{emp.name.replace(' ', '_')}.pdf", pdf.read())
                except Exception as ex:
                    logger.error(f'Bulk zip error for {emp.name}: {ex}')

        zbuf.seek(0)
        return send_file(zbuf, mimetype='application/zip', as_attachment=True,
                         download_name='All_KRA_Reports.zip')
    except Exception as e:
        logger.error(f'Bulk zip error: {e}')
        flash('Could not generate bulk export. Please try again.', 'danger')
        return redirect(url_for('admin_dashboard'))


@app.route('/health')
def health():
    return jsonify(status='Pharma IP v5 Operational', db='ok'), 200


@app.errorhandler(404)
def not_found(e):
    return render_template('login.html'), 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f'500 error: {e}')
    db.session.rollback()
    flash('An internal error occurred. Please try again.', 'danger')
    return redirect(url_for('login'))


if __name__ == '__main__':
    socketio.run(app, debug=False, port=int(os.environ.get('PORT', 5000)))
