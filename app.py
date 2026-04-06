import eventlet
eventlet.monkey_patch()

import os
import logging
import io
import csv
from datetime import date, timedelta, datetime
from functools import wraps
from typing import List, Optional, Dict, Any

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_caching import Cache
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

# Sentry for error tracking (optional - add DSN in env)
if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.environ.get("SENTRY_DSN"),
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.5,
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
IS_PRODUCTION = os.environ.get("RENDER") or os.environ.get("DATABASE_URL")

app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "pharma_secure_key_2024"),
    SESSION_COOKIE_SECURE=bool(IS_PRODUCTION),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=True,
    PREFERRED_URL_SCHEME='https' if IS_PRODUCTION else 'http',
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300}
)

db_url = os.environ.get("DATABASE_URL", "sqlite:///pharma_final.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
csrf = CSRFProtect(app)
limiter = Limiter(app, key_func=get_remote_address, default_limits=["200 per day"])
cache = Cache(app, config={'CACHE_TYPE': 'simple' if not IS_PRODUCTION else 'redis', 'CACHE_REDIS_URL': os.environ.get("REDIS_URL")})


# ─── MODELS ──────────────────────────────────────────────────────────────────

class Employee(db.Model):
    __tablename__ = "employees"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    staff_type    = db.Column(db.String(20), default="picker")
    role          = db.Column(db.String(100), default="Operations Specialist")
    is_admin      = db.Column(db.Boolean, default=False)
    sunday_override = db.Column(db.Boolean, default=False)
    twofa_secret  = db.Column(db.String(32), nullable=True)
    twofa_enabled = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    entries = db.relationship("KPIEntry", backref="owner", lazy="select", cascade="all, delete-orphan")

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class KPIEntry(db.Model):
    __tablename__ = "kpi_entries"
    id               = db.Column(db.Integer, primary_key=True)
    emp_id           = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    sales_bills_open = db.Column(db.Integer, default=0)
    picked           = db.Column(db.Integer, default=0)
    missed           = db.Column(db.Integer, default=0)
    cs_sales_open    = db.Column(db.Integer, default=0)
    packing_done     = db.Column(db.Integer, default=0)
    rack_organized   = db.Column(db.Integer, default=0)
    table_clean      = db.Column(db.Integer, default=0)
    total_time       = db.Column(db.Float, default=0.0)
    checked          = db.Column(db.Integer, default=0)
    errors_found     = db.Column(db.Integer, default=0)
    check_time       = db.Column(db.Float, default=0.0)
    bills            = db.Column(db.Integer, default=0)
    boxes            = db.Column(db.Integer, default=0)
    sweep            = db.Column(db.Float, default=0.0)
    entry_date       = db.Column(db.Date, nullable=False, index=True)
    report_sent      = db.Column(db.Boolean, default=False)
    
    __table_args__ = (
        db.UniqueConstraint("emp_id", "entry_date", name="_emp_date_uc"),
        db.Index("idx_entry_date", "entry_date"),
        db.Index("idx_emp_date", "emp_id", "entry_date"),
        db.Index("idx_emp_id", "emp_id"),
    )

    @property
    def accuracy(self):
        t = (self.picked or 0) + (self.missed or 0)
        return round((self.picked or 0) / t * 100, 1) if t > 0 else 0.0

    @property
    def _sales_bill_effective(self):
        return self.sales_bills_open or self.bills or 0

    @property
    def _total_time_hrs(self):
        return self.total_time or self.sweep or 0

    @property
    def check_rate(self):
        if self.checked and self.checked > 0:
            return round((self.checked - (self.errors_found or 0)) / self.checked * 100, 1)
        return 0


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    action = db.Column(db.String(100))
    target = db.Column(db.String(100))
    details = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def safe_div(a, b, default=0.0):
    try: return a / b if b else default
    except Exception: return default


def grade_from_score(score):
    if score >= 90:   return "EXCELLENT"
    elif score >= 75: return "GOOD"
    elif score >= 60: return "AVERAGE"
    else:             return "NEEDS WORK"


def build_analytics(entries: List[KPIEntry], staff_type: str = "picker") -> Optional[Dict[str, Any]]:
    try:
        if not entries: return None
        tp   = sum(int(e.picked or 0) for e in entries)
        tm   = sum(int(e.missed or 0) for e in entries)
        ti   = tp + tm
        tsb  = sum(int(e.sales_bills_open or e.bills or 0) for e in entries)
        ts   = round(sum(float(e.total_time or e.sweep or 0) for e in entries), 3)
        tck  = sum(int(e.checked or 0) for e in entries)
        ter  = sum(int(e.errors_found or 0) for e in entries)
        tpk  = sum(int(e.packing_done or 0) for e in entries)
        tcs  = sum(int(e.cs_sales_open or 0) for e in entries)
        tro  = sum(int(e.rack_organized or 0) for e in entries)
        ttc  = sum(int(e.table_clean or 0) for e in entries)
        days = len(entries)

        pick_acc   = round(safe_div(tp, ti) * 100, 1)
        pick_speed = round(safe_div(ti, max(ts, 0.001)), 1)
        check_acc  = round(safe_div(tck - ter, tck) * 100, 1) if tck > 0 else 0.0
        packing_eff = round(safe_div(tpk, tsb) * 100, 1) if tsb > 0 else 0
        cs_fulfilment = round(min(safe_div(tpk, tcs) * 100, 100), 1) if tcs > 0 else 0
        
        # Consistency calculation
        daily_accs = [e.accuracy for e in entries if e.accuracy > 0]
        if len(daily_accs) > 1:
            import numpy as np
            std_dev = np.std(daily_accs)
            consistency = max(0, 100 - (std_dev * 2))
        else:
            consistency = 100 if daily_accs else 0

        eff_score = (
            round(pick_acc * 0.6 + min(pick_speed / 200, 1) * 40, 1)
            if staff_type == "picker"
            else round(check_acc * 0.8 + min(tck / 150, 1) * 20, 1)
        )
        grade = grade_from_score(eff_score)

        return dict(
            pick_acc=pick_acc, pick_speed=pick_speed, check_acc=check_acc,
            eff_score=eff_score, grade=grade, days=days,
            tp=tp, tm=tm, tck=tck, ter=ter, tsb=tsb, tpk=tpk, ts=round(ts, 2),
            tpd=tpk, tcs=tcs, tro=tro, ttc=ttc,
            packing_eff=packing_eff, cs_fulfilment=cs_fulfilment,
            consistency=round(consistency, 1)
        )
    except Exception as e:
        logger.error(f"build_analytics error: {e}")
        return None


def get_period_entries(emp_id: int, period: str) -> List[KPIEntry]:
    today = date.today()
    starts = {"day": today, "week": today - timedelta(days=6), "month": today - timedelta(days=29)}
    start = starts.get(period, today)
    try:
        return KPIEntry.query.filter(
            KPIEntry.emp_id == emp_id,
            KPIEntry.entry_date >= start,
            KPIEntry.entry_date <= today
        ).order_by(KPIEntry.entry_date.desc()).all()
    except Exception as e:
        logger.error(f"get_period_entries: {e}")
        return []


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"): return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"): return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def log_audit(action, target, details=""):
    try:
        log = AuditLog(
            admin_id=session.get("user_id"),
            action=action,
            target=target,
            details=details
        )
        db.session.add(log)
        db.session.commit()
    except:
        db.session.rollback()


def init_db():
    with app.app_context():
        try:
            db.create_all()
            if not Employee.query.first():
                admin = Employee(name="Admin", email="admin@pharmaip.com", staff_type="picker", is_admin=True, role="Admin")
                admin.set_password("admin123")
                db.session.add(admin)
                p1 = Employee(name="Rahul Sharma", email="rahul@pharmaip.com", staff_type="picker", role="Picker")
                p1.set_password("test1234")
                db.session.add(p1)
                c1 = Employee(name="Priya Patel", email="priya@pharmaip.com", staff_type="checker", role="Checker")
                c1.set_password("test1234")
                db.session.add(c1)
                db.session.commit()
                logger.info("DB init complete.")
        except Exception as e:
            logger.error(f"DB init error: {e}")
            db.session.rollback()


try:
    init_db()
except Exception as _e:
    logger.error(f"DB init failed: {_e}")


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route('/health')
def health_check():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except:
        db_ok = False
    return jsonify(status="ok", database="up" if db_ok else "down"), 200 if db_ok else 500


@app.route("/")
def index():
    if not session.get("user_id"): return redirect(url_for("login"))
    return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if session.get("user_id"):
        return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))
    if request.method == "POST":
        try:
            email = request.form.get("email", "").lower().strip()
            password = request.form.get("password", "")
            role_choice = request.form.get("staff_type", "").strip()
            user = Employee.query.filter_by(email=email).first()
            
            if not user or not user.check_password(password):
                flash("Invalid Pharma ID or Password.", "danger")
                return render_template("login.html")
            
            if not user.is_admin and role_choice in ("picker", "checker"):
                user.staff_type = role_choice
                db.session.commit()
            
            session.clear()
            session.permanent = True
            session["user_id"] = user.id
            session["user_name"] = user.name
            session["staff_type"] = user.staff_type
            session["is_admin"] = bool(user.is_admin)
            
            logger.info(f"User login successful: {email}")
            return redirect(url_for("admin_dashboard") if user.is_admin else url_for("dashboard"))
        except Exception as e:
            logger.error(f"Login error for email: {email}")
            flash("System error. Please try again.", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    try:
        emp_id = session.get("user_id")
        staff_type = session.get("staff_type", "picker")
        today = date.today()
        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()
        new_personal_best = False

        if request.method == "POST" and not today_entry:
            is_sunday = today.weekday() == 6
            emp = db.session.get(Employee, emp_id)
            if is_sunday and not (emp and emp.sunday_override):
                flash("📅 Sunday is a holiday. No data submission allowed.", "warning")
            else:
                try:
                    def gi(k): return max(0, int(request.form.get(k, 0) or 0))
                    def gf(k): return max(0, float(request.form.get(k, 0) or 0))

                    picked = gi("picked")
                    missed = gi("missed")
                    sales_bills_open = gi("sales_bills_open")
                    cs_sales_open = gi("cs_sales_open")
                    packing_done = gi("packing_done")
                    total_mins = min(gf("total_mins"), 480)
                    check_mins = min(gf("check_mins"), 480)
                    checked = gi("checked")
                    errors_found = min(gi("errors_found"), checked)

                    ne = KPIEntry(
                        emp_id=emp_id,
                        sales_bills_open=sales_bills_open,
                        picked=picked,
                        missed=missed,
                        cs_sales_open=cs_sales_open,
                        packing_done=packing_done,
                        total_time=round(total_mins / 60, 3),
                        checked=checked,
                        errors_found=errors_found,
                        check_time=round(check_mins / 60, 3),
                        entry_date=today
                    )
                    db.session.add(ne)
                    db.session.commit()
                    
                    # Check for personal best
                    all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
                    all_stats = build_analytics(all_entries, staff_type)
                    prev_best = cache.get(f"pb_{emp_id}")
                    if all_stats and (not prev_best or all_stats['eff_score'] > prev_best):
                        cache.set(f"pb_{emp_id}", all_stats['eff_score'], timeout=86400)
                        new_personal_best = True
                    
                    flash("✅ Metrics recorded successfully.", "success")
                    today_entry = ne
                    
                    # Broadcast via socket
                    socketio.emit('entry_update', {'user': session.get('user_name'), 'accuracy': ne.accuracy})
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Dashboard POST: {e}")
                    flash("Error saving metrics.", "danger")

        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        d_stats = build_analytics(get_period_entries(emp_id, "day"), staff_type)
        w_stats = build_analytics(get_period_entries(emp_id, "week"), staff_type)
        m_stats = build_analytics(get_period_entries(emp_id, "month"), staff_type)

        # 7-day trend data
        trend_labels = []
        trend_accuracy = []
        trend_speed = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            e = next((x for x in all_entries if x.entry_date == d), None)
            trend_labels.append(d.strftime("%a %d"))
            if e:
                t = (e.picked or 0) + (e.missed or 0)
                acc = round((e.picked or 0) / t * 100, 1) if t > 0 else 0
                spd = round(t / max(float(e.total_time or 0.001), 0.001), 1)
                trend_accuracy.append(acc)
                trend_speed.append(spd)
            else:
                trend_accuracy.append(None)
                trend_speed.append(None)

        return render_template("dashboard.html",
            user_name=session.get("user_name", "User"),
            staff_type=staff_type,
            today=today,
            today_entry=today_entry,
            d_stats=d_stats,
            w_stats=w_stats,
            m_stats=m_stats,
            recent=all_entries[:14],
            trend_labels=trend_labels,
            trend_accuracy=trend_accuracy,
            trend_speed=trend_speed,
            total_entries=len(all_entries),
            new_personal_best=new_personal_best
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return render_template("dashboard.html",
            user_name="User", staff_type="picker", today=date.today(),
            today_entry=None, d_stats=None, w_stats=None, m_stats=None,
            recent=[], trend_labels=[], trend_accuracy=[], trend_speed=[], total_entries=0,
            new_personal_best=False)


@app.route("/admin_dashboard")
@admin_required
@cache.cached(timeout=300, key_prefix='admin_dashboard')
def admin_dashboard():
    try:
        employees = Employee.query.filter_by(is_admin=False).options(db.joinedload(Employee.entries)).all()
        rows = []
        today = date.today()
        for emp in employees:
            ents = emp.entries  # Already loaded via joinedload
            stats = build_analytics(ents, emp.staff_type)
            week_ents = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
            week_stats = build_analytics(week_ents, emp.staff_type)
            rows.append({
                'emp': emp,
                'stats': stats,
                'week_stats': week_stats,
                'count': len(ents),
                'last_entry': ents[0].entry_date if ents else None,
            })

        all_ents = KPIEntry.query.all()
        total_picked = sum(e.picked or 0 for e in all_ents)
        total_entries = len(all_ents)
        active_today = KPIEntry.query.filter_by(entry_date=today).count()

        return render_template("admin.html",
            rows=rows,
            total_picked=total_picked,
            total_entries=total_entries,
            active_today=active_today,
            emp_count=len(employees),
            today=today
        )
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        flash("Error loading admin dashboard.", "danger")
        return render_template("admin.html", rows=[], total_picked=0,
                               total_entries=0, active_today=0, emp_count=0, today=date.today())


@app.route("/admin/toggle_sunday/<int:emp_id>", methods=["POST"])
@admin_required
def admin_toggle_sunday(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if emp and not emp.is_admin:
            emp.sunday_override = not emp.sunday_override
            db.session.commit()
            cache.delete('admin_dashboard')
            status = "enabled" if emp.sunday_override else "disabled"
            log_audit("sunday_toggle", emp.name, f"Sunday override {status}")
            flash(f"Sunday data entry {status} for {emp.name}.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"toggle_sunday: {e}")
        flash("Error updating Sunday override.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/add_user", methods=["POST"])
@admin_required
def admin_add_user():
    try:
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        staff_type = request.form.get("staff_type", "picker").strip()

        if not name or not email or not password:
            flash("All fields required.", "danger")
            return redirect(url_for("admin_dashboard"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for("admin_dashboard"))
        if Employee.query.filter_by(email=email).first():
            flash(f"Email '{email}' already exists.", "warning")
            return redirect(url_for("admin_dashboard"))

        emp = Employee(name=name, email=email, staff_type=staff_type, role=f"Operations {staff_type.title()}")
        emp.set_password(password)
        db.session.add(emp)
        db.session.commit()
        cache.delete('admin_dashboard')
        log_audit("add_user", name, f"Added as {staff_type}")
        flash(f"✅ {name} added successfully.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"add_user: {e}")
        flash("Error adding user.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/edit_user/<int:emp_id>", methods=["POST"])
@admin_required
def admin_edit_user(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp or emp.is_admin:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        staff_type = request.form.get("staff_type", emp.staff_type).strip()
        new_password = request.form.get("new_password", "").strip()

        if name:
            emp.name = name
        if email and email != emp.email:
            existing = Employee.query.filter_by(email=email).first()
            if existing and existing.id != emp_id:
                flash(f"Email '{email}' already taken.", "warning")
                return redirect(url_for("admin_dashboard"))
            emp.email = email
        if staff_type in ("picker", "checker"):
            emp.staff_type = staff_type
            emp.role = f"Operations {staff_type.title()}"
        if new_password:
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
                return redirect(url_for("admin_dashboard"))
            emp.set_password(new_password)

        db.session.commit()
        cache.delete('admin_dashboard')
        log_audit("edit_user", name, f"Updated by admin")
        flash(f"✅ {emp.name} updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"edit_user: {e}")
        flash("Error updating user.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_user/<int:emp_id>", methods=["POST"])
@admin_required
def admin_delete_user(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if emp and not emp.is_admin:
            name = emp.name
            db.session.delete(emp)
            db.session.commit()
            cache.delete('admin_dashboard')
            log_audit("delete_user", name, "User removed")
            flash(f"🗑️ {name} removed.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_user: {e}")
        flash("Error deleting user.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export_csv")
@admin_required
def admin_export_csv():
    try:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee", "Email", "Type", "Date", "Picked", "Missed",
                         "Accuracy%", "SalesBills", "Packing", "Checked", "Errors",
                         "TotalTimeHrs", "CheckTimeHrs"])
        employees = Employee.query.filter_by(is_admin=False).all()
        for emp in employees:
            entries = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date).all()
            for e in entries:
                t = (e.picked or 0) + (e.missed or 0)
                acc = round((e.picked or 0) / t * 100, 1) if t > 0 else 0
                writer.writerow([
                    emp.name, emp.email, emp.staff_type, e.entry_date,
                    e.picked, e.missed, acc,
                    e.sales_bills_open, e.packing_done,
                    e.checked, e.errors_found,
                    round(e.total_time or 0, 2), round(e.check_time or 0, 2)
                ])
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=pharma_kpi_{date.today()}.csv"}
        )
    except Exception as e:
        logger.error(f"export_csv: {e}")
        flash("Error exporting data.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/export_pdf")
@login_required
def export_pdf():
    try:
        from utils import generate_visual_pdf
        emp_id = session.get("user_id")
        emp = db.session.get(Employee, emp_id)
        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
        
        payload = {
            "all_stats": build_analytics(all_entries, emp.staff_type),
            "day_stats": build_analytics(get_period_entries(emp_id, "day"), emp.staff_type),
            "week_stats": build_analytics(get_period_entries(emp_id, "week"), emp.staff_type),
            "month_stats": build_analytics(get_period_entries(emp_id, "month"), emp.staff_type),
            "staff_type": emp.staff_type,
            "all_entries": all_entries[-30:],
        }
        
        pdf_buffer = generate_visual_pdf(emp.name, payload)
        return Response(
            pdf_buffer.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=KRA_{emp.name}_{date.today()}.pdf"}
        )
    except Exception as e:
        logger.error(f"PDF export error: {e}")
        flash("Error generating PDF report.", "danger")
        return redirect(url_for("dashboard"))


@app.route("/api/stats/<int:emp_id>")
@login_required
def api_stats(emp_id):
    if not session.get("is_admin") and session.get("user_id") != emp_id:
        return jsonify(error="Unauthorized"), 403
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            return jsonify(error="Not found"), 404
        entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
        stats = build_analytics(entries, emp.staff_type)
        return jsonify(stats=stats, name=emp.name, staff_type=emp.staff_type)
    except Exception as e:
        logger.error(f"api_stats: {e}")
        return jsonify(error="Server error"), 500


@socketio.on('connect')
def handle_connect():
    emit('connected', {'message': 'Connected to KPI tracker'})


# ─── ERROR HANDLERS ──────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return redirect(url_for("login"))

@app.errorhandler(500)
def server_error(e):
    logger.error(f"500: {e}")
    return redirect(url_for("login"))


# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
