from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import zipfile
import io
import atexit

from utils import generate_visual_pdf, calculate_kra_grade

app = Flask(__name__)
app.secret_key = "ip_pharma_ultra_secure_99"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pharma_v3.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ─────────────────────────────────────────
#  MODELS  (single source of truth)
# ─────────────────────────────────────────
class Employee(db.Model):
    __tablename__ = 'employees'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(100), default="Operations Specialist")
    is_admin      = db.Column(db.Boolean, default=False)
    entries       = db.relationship('KPIEntry', backref='owner', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class KPIEntry(db.Model):
    __tablename__ = 'kpi_entries'
    id           = db.Column(db.Integer, primary_key=True)
    emp_id       = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    bills        = db.Column(db.Integer, default=0)
    picked       = db.Column(db.Integer, default=0)
    missed       = db.Column(db.Integer, default=0)
    boxes        = db.Column(db.Integer, default=0)   # CS Opened in Purchase
    sweep        = db.Column(db.Float,   default=0.0)  # Sweeping in decimal hours
    entry_date   = db.Column(db.Date,    nullable=False, index=True)
    report_sent  = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint('emp_id', 'entry_date', name='_emp_date_uc'),)

    @property
    def accuracy(self):
        total = self.picked + self.missed
        return round((self.picked / total * 100), 1) if total > 0 else 0.0


# ─────────────────────────────────────────
#  48-HR AUTOMATION ENGINE
# ─────────────────────────────────────────
def auto_report_job():
    with app.app_context():
        target_date = date.today() - timedelta(days=2)
        pending = KPIEntry.query.filter_by(entry_date=target_date, report_sent=False).all()
        for entry in pending:
            emp = Employee.query.get(entry.emp_id)
            if emp:
                print(f"[48-HR AUTO] Processing KRA for {emp.name} — {target_date}")
            entry.report_sent = True
        db.session.commit()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(func=auto_report_job, trigger="interval", hours=24, id="auto_report")
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            flash("Access denied: Admin only.", "danger")
            return redirect(url_for('submit_kpi'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def login():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard' if session.get('is_admin') else 'submit_kpi'))

    if request.method == "POST":
        email = request.form.get('email', '').lower().strip()
        user  = Employee.query.filter_by(email=email).first()

        if user and user.check_password(request.form.get('password', '')):
            session['user_id']   = user.id
            session['user_name'] = user.name
            session['is_admin']  = user.is_admin
            return redirect(url_for('admin_dashboard' if user.is_admin else 'submit_kpi'))

        flash("Invalid credentials. Please try again.", "danger")

    return render_template("login.html")


@app.route("/submit", methods=["GET", "POST"])
@login_required
def submit_kpi():
    if request.method == "POST":
        try:
            picked     = int(request.form.get('picked', 0))
            missed     = int(request.form.get('missed', 0))
            sweep_mins = float(request.form.get('sweep_mins', 0))
            sweep_hrs  = round(sweep_mins / 60, 2)

            new_entry = KPIEntry(
                emp_id     = session['user_id'],
                bills      = int(request.form.get('bills', 0)),
                picked     = picked,
                missed     = missed,
                boxes      = int(request.form.get('boxes', 0)),
                sweep      = sweep_hrs,
                entry_date = datetime.strptime(request.form['date'], "%Y-%m-%d").date()
            )
            db.session.add(new_entry)
            db.session.commit()

            # Real-time admin alert on critical accuracy drop
            total = picked + missed
            eff   = round((picked / total * 100), 1) if total > 0 else 0.0
            if eff < 85:
                socketio.emit('admin_alert', {
                    'name': session['user_name'],
                    'eff' : eff,
                    'msg' : 'Critical Accuracy Drop'
                })

            flash("Metrics recorded successfully.", "success")

        except Exception:
            db.session.rollback()
            flash("Entry already exists for this date.", "warning")

    return render_template("submit.html", user_name=session['user_name'], today=date.today())


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    employees = Employee.query.filter_by(is_admin=False).all()
    data = []
    for emp in employees:
        logs = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.desc()).all()
        if logs:
            avg_acc = round(sum(e.accuracy for e in logs) / len(logs), 1)
        else:
            avg_acc = 0.0
        grade, _ = calculate_kra_grade(
            sum(e.picked for e in logs),
            sum(e.missed for e in logs),
            sum(e.boxes  for e in logs)
        )
        data.append({
            'emp'  : emp,
            'logs' : logs,
            'avg'  : avg_acc,
            'grade': grade,
            'count': len(logs),
            'total_boxes': sum(e.boxes for e in logs),
            'total_sweep': round(sum(e.sweep for e in logs), 2),
        })

    return render_template("admin.html", data=data)


@app.route("/download/<int:emp_id>")
@login_required
@admin_required
def download_report(emp_id):
    emp  = Employee.query.get_or_404(emp_id)
    logs = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()

    if not logs:
        flash("No data available for this employee.", "warning")
        return redirect(url_for('admin_dashboard'))

    payload = {
        'bills'      : sum(e.bills  for e in logs),
        'picked'     : sum(e.picked for e in logs),
        'missed'     : sum(e.missed for e in logs),
        'cs_purchase': sum(e.boxes  for e in logs),
        'sweep_hours': round(sum(e.sweep for e in logs), 2),
        'entries'    : logs,
    }
    pdf_buffer = generate_visual_pdf(emp.name, payload)
    return send_file(
        pdf_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"KRA_{emp.name.replace(' ', '_')}.pdf"
    )


@app.route("/bulk_zip")
@login_required
@admin_required
def bulk_zip():
    employees = Employee.query.filter_by(is_admin=False).all()
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for emp in employees:
            logs = KPIEntry.query.filter_by(emp_id=emp.id).all()
            if not logs:
                continue
            payload = {
                'bills'      : sum(e.bills  for e in logs),
                'picked'     : sum(e.picked for e in logs),
                'missed'     : sum(e.missed for e in logs),
                'cs_purchase': sum(e.boxes  for e in logs),
                'sweep_hours': round(sum(e.sweep for e in logs), 2),
                'entries'    : logs,
            }
            pdf = generate_visual_pdf(emp.name, payload)
            zf.writestr(f"KRA_{emp.name.replace(' ', '_')}.pdf", pdf.read())

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name="All_KRA_Reports.zip"
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route("/health")
def health_check():
    return {"status": "Pharma IP System Operational", "version": "3.0"}, 200


# ─────────────────────────────────────────
#  DB INIT + SEED
# ─────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

        # ── ADMIN ──────────────────────────────
        if not Employee.query.filter_by(email="admin@pharmaip.com").first():
            admin = Employee(name="System Admin", email="admin@pharmaip.com", is_admin=True)
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
            print("✅ Admin seeded:    admin@pharmaip.com  /  admin123")

        # ── TEST EMPLOYEE 1 ────────────────────
        if not Employee.query.filter_by(email="rahul@pharmaip.com").first():
            emp1 = Employee(name="Rahul Sharma", email="rahul@pharmaip.com",
                            role="Operations Specialist")
            emp1.set_password("test1234")
            db.session.add(emp1)
            db.session.flush()   # get emp1.id before commit

            # Sample KPI entries for last 5 days
            sample_entries_1 = [
                KPIEntry(emp_id=emp1.id, bills=42, picked=198, missed=4,
                         boxes=12, sweep=1.5,
                         entry_date=date.today() - timedelta(days=4)),
                KPIEntry(emp_id=emp1.id, bills=37, picked=210, missed=2,
                         boxes=9,  sweep=1.2,
                         entry_date=date.today() - timedelta(days=3)),
                KPIEntry(emp_id=emp1.id, bills=45, picked=185, missed=8,
                         boxes=14, sweep=1.8,
                         entry_date=date.today() - timedelta(days=2)),
                KPIEntry(emp_id=emp1.id, bills=39, picked=202, missed=3,
                         boxes=11, sweep=1.3,
                         entry_date=date.today() - timedelta(days=1)),
                KPIEntry(emp_id=emp1.id, bills=50, picked=220, missed=1,
                         boxes=16, sweep=1.0,
                         entry_date=date.today()),
            ]
            db.session.add_all(sample_entries_1)
            db.session.commit()
            print("✅ Employee 1 seeded: rahul@pharmaip.com  /  test1234")

        # ── TEST EMPLOYEE 2 ────────────────────
        if not Employee.query.filter_by(email="priya@pharmaip.com").first():
            emp2 = Employee(name="Priya Patel", email="priya@pharmaip.com",
                            role="Operations Specialist")
            emp2.set_password("test1234")
            db.session.add(emp2)
            db.session.flush()

            # Sample KPI entries — slightly lower accuracy for contrast
            sample_entries_2 = [
                KPIEntry(emp_id=emp2.id, bills=31, picked=160, missed=18,
                         boxes=8,  sweep=2.0,
                         entry_date=date.today() - timedelta(days=4)),
                KPIEntry(emp_id=emp2.id, bills=28, picked=172, missed=12,
                         boxes=7,  sweep=1.7,
                         entry_date=date.today() - timedelta(days=3)),
                KPIEntry(emp_id=emp2.id, bills=33, picked=190, missed=6,
                         boxes=10, sweep=1.5,
                         entry_date=date.today() - timedelta(days=2)),
                KPIEntry(emp_id=emp2.id, bills=29, picked=168, missed=14,
                         boxes=6,  sweep=1.9,
                         entry_date=date.today() - timedelta(days=1)),
                KPIEntry(emp_id=emp2.id, bills=35, picked=195, missed=9,
                         boxes=11, sweep=1.4,
                         entry_date=date.today()),
            ]
            db.session.add_all(sample_entries_2)
            db.session.commit()
            print("✅ Employee 2 seeded: priya@pharmaip.com  /  test1234")

    socketio.run(app, debug=True, port=5000)
