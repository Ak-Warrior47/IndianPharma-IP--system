import eventlet
eventlet.monkey_patch()  # MUST BE ABSOLUTE FIRST LINE

import os, logging, zipfile, io, atexit
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix

# ══════════════════════════════════════════════════
#  1. APP & LOGGING SETUP
# ══════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ══════════════════════════════════════════════════
#  2. RENDER INFRASTRUCTURE
# ══════════════════════════════════════════════════
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

IS_PRODUCTION = os.environ.get("RENDER") or os.environ.get("DATABASE_URL")

# Warn loudly if no SECRET_KEY is set in production — sessions will be insecure
if IS_PRODUCTION and not os.environ.get("SECRET_KEY"):
    logger.warning("⚠️  SECRET_KEY env var is not set! Using insecure default. Set it in Render environment variables.")

app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "pharma_secure_key_2024_local_only"),
    SESSION_COOKIE_SECURE=bool(IS_PRODUCTION),   # True on Render, False locally
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_NAME='pharma_session',        # Explicit name avoids stale cookie conflicts
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_REFRESH_EACH_REQUEST=True,
    PREFERRED_URL_SCHEME='https' if IS_PRODUCTION else 'http',
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300}
)

# Database: Postgres on Render, SQLite locally
db_url = os.environ.get("DATABASE_URL", "sqlite:///pharma_final.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url

db = SQLAlchemy(app)
_cors_origins = os.environ.get("CORS_ORIGIN", "*") if not IS_PRODUCTION else os.environ.get("CORS_ORIGIN", "*")
socketio = SocketIO(app, cors_allowed_origins=_cors_origins, async_mode="eventlet")

# ══════════════════════════════════════════════════
#  3. MODELS
# ══════════════════════════════════════════════════
class Employee(db.Model):
    __tablename__ = "employees"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    staff_type    = db.Column(db.String(20), default="picker")   # picker | checker
    role          = db.Column(db.String(100), default="Operations Specialist")
    is_admin      = db.Column(db.Boolean, default=False)
    entries       = db.relationship("KPIEntry", backref="owner", lazy="select",
                                    cascade="all, delete-orphan")

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class KPIEntry(db.Model):
    __tablename__     = "kpi_entries"
    id                = db.Column(db.Integer, primary_key=True)
    emp_id            = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    # ── New parameters (in order) ──
    sales_bills_open  = db.Column(db.Integer, default=0)   # Sales Bill Packed (Urgent Bill)
    picked            = db.Column(db.Integer, default=0)   # Item Picked
    missed            = db.Column(db.Integer, default=0)   # Item Missed
    cs_sales_open     = db.Column(db.Integer, default=0)   # CS Sales Open
    packing_done      = db.Column(db.Integer, default=0)   # Packing Done
    rack_organized    = db.Column(db.Integer, default=0)   # Rack Organized (0/1)
    table_clean       = db.Column(db.Integer, default=0)   # Table Clean (0/1)
    total_time        = db.Column(db.Float,   default=0.0) # Total Time hours (replaces sweep)
    # ── Checker fields ──
    checked           = db.Column(db.Integer, default=0)
    errors_found      = db.Column(db.Integer, default=0)
    check_time        = db.Column(db.Float,   default=0.0)
    # ── Legacy columns kept for backward-compat with old rows ──
    bills             = db.Column(db.Integer, default=0)
    boxes             = db.Column(db.Integer, default=0)
    sweep             = db.Column(db.Float,   default=0.0)
    entry_date        = db.Column(db.Date,    nullable=False, index=True)
    report_sent       = db.Column(db.Boolean, default=False)
    __table_args__    = (db.UniqueConstraint("emp_id", "entry_date", name="_emp_date_uc"),)

    @property
    def accuracy(self):
        t = self.picked + self.missed
        return round(self.picked / t * 100, 1) if t > 0 else 0.0

    @property
    def effective_time(self):
        return (self.total_time or 0) if (self.total_time or 0) > 0 else (self.sweep or 0)

    @property
    def effective_cs(self):
        return (self.cs_sales_open or 0) if (self.cs_sales_open or 0) > 0 else (self.boxes or 0)

    @property
    def effective_bills(self):
        return (self.sales_bills_open or 0) if (self.sales_bills_open or 0) > 0 else (self.bills or 0)

    @property
    def pick_speed(self):
        hrs = self.effective_time if self.effective_time > 0 else 1
        return round((self.picked + self.missed) / hrs, 1)

    @property
    def check_rate(self):
        return round(self.errors_found / self.checked * 100, 1) if self.checked > 0 else 0.0


# ══════════════════════════════════════════════════
#  4. DECORATORS
# ══════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get("user_id"):   # .get() safely handles missing OR None
            session.clear()              # Wipe any corrupt partial session
            flash("Please sign in.", "warning")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get("is_admin"):
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return decorated

# ══════════════════════════════════════════════════
#  5. ANALYTICS ENGINE (Full File 2 version)
# ══════════════════════════════════════════════════
def safe_div(a, b, default=0.0):
    try:
        return a / b if b else default
    except Exception:
        return default


def build_analytics(entries, staff_type="picker"):
    try:
        if not entries:
            return None

        tp   = sum(int(e.picked or 0)                      for e in entries)
        tm   = sum(int(e.missed or 0)                      for e in entries)
        ti   = tp + tm
        # Sales Bills Open (new) with legacy bills fallback
        tsb  = sum(int(e.effective_bills)                  for e in entries)
        # CS Sales Open (new) with legacy boxes fallback
        tcs  = sum(int(e.effective_cs)                     for e in entries)
        # Packing Done
        tpd  = sum(int(e.packing_done or 0)               for e in entries)
        # Rack Organized & Table Clean (daily compliance: count of days done)
        tro  = sum(int(e.rack_organized or 0)              for e in entries)
        ttc  = sum(int(e.table_clean or 0)                 for e in entries)
        # Total Time (new) with legacy sweep fallback
        ts   = round(sum(float(e.effective_time)           for e in entries), 3)
        # Checker fields
        tck  = sum(int(e.checked or 0)                     for e in entries)
        ter  = sum(int(e.errors_found or 0)                for e in entries)
        tct  = round(sum(float(e.check_time or 0)          for e in entries), 3)

        days = len(entries)
        # Workspace compliance rate (rack + table cleanliness)
        workspace_score = round(safe_div(tro + ttc, days * 2) * 100, 1) if days > 0 else 0.0

        pick_acc   = round(safe_div(tp, ti) * 100, 1)
        pick_speed = round(safe_div(ti, ts), 1)

        correct_checks = max(0, tck - ter)
        check_acc  = round(safe_div(correct_checks, tck) * 100, 1)
        error_rate = round(safe_div(ter, tck) * 100, 1)
        ck_speed   = round(safe_div(tck, tct), 1)

        # Packing efficiency: packing_done / (picked + missed) — how much was packed vs picked
        pack_eff   = round(safe_div(tpd, ti) * 100, 1) if ti > 0 else 0.0

        # ── EFFICIENCY SCORE FORMULA ──────────────────────────────────────────
        # PICKER:
        #   Base = (pick_acc/100)*55 + min(pick_speed/200,1)*30
        #   Bonus = workspace_score/100*10 + min(pack_eff/100,1)*5
        #   Total = min(100, Base + Bonus)
        #   Benchmark: 98% acc + 200/hr speed + 100% workspace + 100% pack = 100
        #
        # CHECKER:
        #   Base = (check_acc/100)*65 + min(ck_speed/150,1)*25
        #   Bonus = workspace_score/100*10
        #   Total = min(100, Base + Bonus)
        #   Benchmark: 97% clean rate + 150/hr speed + 100% workspace = 100
        # ─────────────────────────────────────────────────────────────────────
        if staff_type == "checker":
            base_eff        = (check_acc / 100 * 65) + (min(safe_div(ck_speed, 150), 1) * 25)
            bonus_eff       = (workspace_score / 100 * 10)
            eff_score       = round(min(100, base_eff + bonus_eff), 1)
            potential_items = int(150.0 * tct) if tct > 0 else tck
            potential_eff   = round(min(100, eff_score + max(0, (100 - check_acc) * 0.5)), 1)
            gap_items       = max(0, potential_items - tck)
        else:
            base_eff        = (pick_acc / 100 * 55) + (min(safe_div(pick_speed, 200), 1) * 30)
            bonus_eff       = (workspace_score / 100 * 10) + (min(safe_div(pack_eff, 100), 1) * 5)
            eff_score       = round(min(100, base_eff + bonus_eff), 1)
            potential_items = int(200.0 * ts)
            potential_eff   = round(min(100, eff_score + max(0, (98 - pick_acc) * 0.5 + (200 - pick_speed) * 0.1)), 1)
            gap_items       = max(0, potential_items - ti)

        # ── 4-TIER GRADING ────────────────────────────────────────────────────
        # PICKER: ELITE = 98%+ acc AND workspace_score>=80
        #         PROFICIENT = 95%+ acc
        #         SATISFACTORY = 88%+ acc
        #         RE-TRAINING = <88% acc
        # CHECKER: ELITE = 97%+ clean rate AND workspace_score>=80
        #          PROFICIENT = 94%+ clean rate
        #          SATISFACTORY = 87%+ clean rate
        #          RE-TRAINING = <87% clean rate
        # ─────────────────────────────────────────────────────────────────────
        if staff_type == "picker":
            if pick_acc >= 98 and workspace_score >= 80:
                grade, fb = "ELITE",        "Exceptional pick accuracy & workspace standards. Gold Standard."
            elif pick_acc >= 95:
                grade, fb = "PROFICIENT",   "Meets standard pharma pick accuracy requirements."
            elif pick_acc >= 88:
                grade, fb = "SATISFACTORY", "Acceptable. Focus on reducing missed picks & workspace compliance."
            else:
                grade, fb = "RE-TRAINING",  "Pick accuracy below safety threshold. Intervention required."
        else:
            if check_acc >= 97 and workspace_score >= 80:
                grade, fb = "ELITE",        "Exceptional verification accuracy & workspace standards. Zero-error standard met."
            elif check_acc >= 94:
                grade, fb = "PROFICIENT",   "Good check accuracy. Minor improvements remain."
            elif check_acc >= 87:
                grade, fb = "SATISFACTORY", "Acceptable check rate. Increase error detection."
            else:
                grade, fb = "RE-TRAINING",  "Verification accuracy below threshold. Re-training needed."

        # ── CONSISTENCY (variance-based) ──
        if staff_type == "checker":
            daily_accs = [round(safe_div(max(0, (e.checked or 0) - (e.errors_found or 0)),
                                         (e.checked or 0)) * 100, 1)
                          for e in entries if (e.checked or 0) > 0]
        else:
            daily_accs = [e.accuracy for e in entries if (e.picked + e.missed) > 0]

        if len(daily_accs) > 1:
            mean_a      = sum(daily_accs) / len(daily_accs)
            variance    = sum((x - mean_a) ** 2 for x in daily_accs) / len(daily_accs)
            consistency = round(max(0, 100 - (variance ** 0.5) * 2), 1)
        else:
            consistency = 100.0 if daily_accs else 0.0

        # ── TREND ──
        trend = "stable"
        if len(entries) >= 4:
            if staff_type == "checker":
                def _acc(e): return round(safe_div(max(0, (e.checked or 0) - (e.errors_found or 0)),
                                                   (e.checked or 0)) * 100, 1)
            else:
                def _acc(e): return e.accuracy
            recent_avg = sum(_acc(e) for e in entries[:2]) / 2
            older_avg  = sum(_acc(e) for e in entries[-2:]) / 2
            if recent_avg > older_avg + 2:   trend = "improving"
            elif recent_avg < older_avg - 2: trend = "declining"

        return dict(
            tp=tp, tm=tm, ti=ti, tsb=tsb, tcs=tcs, tpd=tpd,
            tro=tro, ttc=ttc, ts=ts, tck=tck, ter=ter, tct=tct,
            pick_acc=pick_acc, pick_speed=pick_speed,
            check_acc=check_acc, error_rate=error_rate, ck_speed=ck_speed,
            pack_eff=pack_eff, workspace_score=workspace_score,
            eff_score=eff_score, grade=grade, feedback=fb,
            potential_items=potential_items, potential_eff=potential_eff,
            gap_items=gap_items, consistency=consistency, trend=trend,
            days=days
        )
    except Exception as e:
        logger.error(f"build_analytics error: {e}")
        return None


def get_period_entries(emp_id, period):
    today  = date.today()
    starts = {
        "day":   today,
        "week":  today - timedelta(days=6),
        "month": today - timedelta(days=29)
    }
    start = starts.get(period, today)
    try:
        return KPIEntry.query.filter(
            KPIEntry.emp_id     == emp_id,
            KPIEntry.entry_date >= start,
            KPIEntry.entry_date <= today
        ).order_by(KPIEntry.entry_date.desc()).all()
    except Exception as e:
        logger.error(f"get_period_entries: {e}")
        return []


def build_leaderboard(staff_type=None):
    try:
        query = Employee.query.filter_by(is_admin=False)
        if staff_type:
            query = query.filter_by(staff_type=staff_type)
        employees = query.all()
        if not employees:
            return []

        # Load ALL entries in one query instead of N queries (N+1 fix)
        emp_ids = [emp.id for emp in employees]
        all_entries = KPIEntry.query.filter(KPIEntry.emp_id.in_(emp_ids)).all()
        entries_by_emp = {}
        for e in all_entries:
            entries_by_emp.setdefault(e.emp_id, []).append(e)

        lb = []
        for emp in employees:
            entries = entries_by_emp.get(emp.id, [])
            stats   = build_analytics(entries, emp.staff_type)
            if not stats:
                continue
            lb.append({
                "id":              emp.id,
                "name":            emp.name,
                "email":           emp.email,
                "staff_type":      emp.staff_type,
                "role":            emp.role,
                "score":           stats["eff_score"],
                "grade":           stats["grade"],
                "pick_acc":        stats["pick_acc"],
                "pick_speed":      stats["pick_speed"],
                "check_acc":       stats["check_acc"],
                "error_rate":      stats["error_rate"],
                "ck_speed":        stats["ck_speed"],
                "consistency":     stats["consistency"],
                "trend":           stats["trend"],
                "days":            stats["days"],
                "tp":              stats["tp"],
                "tm":              stats["tm"],
                "pack_eff":        stats["pack_eff"],
                "workspace_score": stats["workspace_score"],
                "potential_eff":   stats["potential_eff"],
                "gap_items":       stats["gap_items"],
            })
        lb.sort(key=lambda x: x["score"], reverse=True)
        return lb
    except Exception as e:
        logger.error(f"build_leaderboard: {e}")
        return []


# ══════════════════════════════════════════════════
#  6. SCHEDULER (from File 2)
# ══════════════════════════════════════════════════
def start_scheduler():
    # Guard: only start once — gunicorn spawns multiple workers, avoid duplicate schedulers
    if os.environ.get("SCHEDULER_STARTED"):
        return
    os.environ["SCHEDULER_STARTED"] = "1"

    def scheduled_task():
        with app.app_context():
            try:
                target  = date.today() - timedelta(days=2)
                pending = KPIEntry.query.filter_by(entry_date=target, report_sent=False).all()
                for e in pending:
                    e.report_sent = True
                db.session.commit()
                logger.info("Scheduler: archived old reports.")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Scheduler job error: {e}")
    try:
        s = BackgroundScheduler(daemon=True)
        s.add_job(scheduled_task, "interval", hours=12, id="daily_cleanup", replace_existing=True)
        s.start()
        atexit.register(lambda: s.shutdown(wait=False))
        logger.info("Scheduler started.")
    except Exception as e:
        logger.error(f"Scheduler failed to start: {e}")


# ══════════════════════════════════════════════════
#  7. DATABASE INIT + SEED DATA (from File 2)
# ══════════════════════════════════════════════════
def init_db():
    with app.app_context():
        try:
            db.create_all()

            def make(name, email, pw, stype="picker", admin=False):
                ex = Employee.query.filter_by(email=email).first()
                if ex:
                    return ex
                emp = Employee(
                    name=name, email=email, staff_type=stype, is_admin=admin,
                    role="Admin" if admin else f"Operations {stype.title()}"
                )
                emp.set_password(pw)
                db.session.add(emp)
                db.session.flush()
                return emp

            make("System Admin", "admin@pharmaip.com", "admin123", admin=True)

            p1 = make("Rahul Sharma", "rahul@pharmaip.com", "test1234", stype="picker")
            if p1 and not KPIEntry.query.filter_by(emp_id=p1.id).first():
                # sales_bills_open, picked, missed, cs_sales_open, packing_done, rack, table, total_time_hrs
                for d, sb, pk, ms, cs, pd, ro, tc, tt in [
                    (6,40,195,5,10,180,1,1,1.5),(5,42,210,2,12,200,1,1,1.4),
                    (4,38,188,8,9,170,0,1,1.6), (3,45,220,1,14,210,1,1,1.3),
                    (2,41,200,4,11,185,1,0,1.5),(1,44,215,3,13,205,1,1,1.4),
                    (0,46,225,2,15,215,1,1,1.3)]:
                    db.session.add(KPIEntry(
                        emp_id=p1.id, sales_bills_open=sb, picked=pk, missed=ms,
                        cs_sales_open=cs, packing_done=pd, rack_organized=ro,
                        table_clean=tc, total_time=tt,
                        entry_date=date.today() - timedelta(days=d)))

            c1 = make("Priya Patel", "priya@pharmaip.com", "test1234", stype="checker")
            if c1 and not KPIEntry.query.filter_by(emp_id=c1.id).first():
                # sales_bills_open, picked, missed, cs_sales_open, packing_done, ro, tc, total_time, checked, errors, check_mins
                for d, sb, pk, ms, cs, pd, ro, tc, tt, ck, er, cm in [
                    (6,30,160,18,8,150,1,1,2.0,178,14,90),(5,28,172,12,7,160,1,1,1.8,184,10,85),
                    (4,33,190,6,10,175,1,0,1.6,196,6,80), (3,29,168,14,6,155,0,1,1.9,182,12,88),
                    (2,35,195,9,11,180,1,1,1.5,204,8,82), (1,31,180,10,9,165,1,1,1.7,190,9,86),
                    (0,36,200,7,12,185,1,1,1.4,207,7,78)]:
                    db.session.add(KPIEntry(
                        emp_id=c1.id, sales_bills_open=sb, picked=pk, missed=ms,
                        cs_sales_open=cs, packing_done=pd, rack_organized=ro,
                        table_clean=tc, total_time=tt, checked=ck,
                        errors_found=er, check_time=round(cm/60, 3),
                        entry_date=date.today() - timedelta(days=d)))

            db.session.commit()
            logger.info("DB init complete.")
        except Exception as e:
            logger.error(f"DB init error: {e}")
            db.session.rollback()


try:
    init_db()
except Exception as _init_err:
    logger.error(f"Database init failed: {_init_err}")

try:
    start_scheduler()
except Exception as _sched_err:
    logger.error(f"Scheduler start failed: {_sched_err}")


# ══════════════════════════════════════════════════
#  8. ROUTES
# ══════════════════════════════════════════════════
@app.route('/health')
def health_check():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify(status="ok"), 200
    except Exception as e:
        logger.error(f"Health check DB failure: {e}")
        return jsonify(status="db_error"), 503


@app.route("/")
def index():
    # Hard stop — never redirect to self
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login"))
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in go straight to destination — never loop back to /login
    if session.get("user_id"):
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        # Basic brute-force guard: track failed attempts in session
        attempts = session.get("login_attempts", 0)
        if attempts >= 10:
            flash("Too many failed login attempts. Please wait and try again.", "danger")
            return render_template("login.html")
        try:
            email       = request.form.get("email", "").lower().strip()
            password    = request.form.get("password", "")
            role_choice = request.form.get("staff_type", "").strip()
            user        = Employee.query.filter_by(email=email).first()

            if not user or not user.check_password(password):
                # Use constant-time comparison path to avoid timing attacks that reveal valid emails
                if not user:
                    check_password_hash("dummy", password)  # consume same time even if user not found
                session["login_attempts"] = session.get("login_attempts", 0) + 1
                flash("Invalid Pharma ID or Password.", "danger")
                return render_template("login.html")

            # Allow staff to pick their role at login
            if not user.is_admin and role_choice in ("picker", "checker"):
                user.staff_type = role_choice
                db.session.commit()

            # Clear OLD session first to wipe any stale/corrupt cookie and login attempt counter
            session.clear()
            session.permanent = True
            session["user_id"]    = user.id
            session["user_name"]  = user.name
            session["staff_type"] = user.staff_type
            session["is_admin"]   = bool(user.is_admin)

            logger.info(f"Login OK: {user.email} admin={user.is_admin}")

            if user.is_admin:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))

        except Exception as e:
            logger.error(f"Login error: {e}")
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
        emp_id     = session.get("user_id")
        if not emp_id:
            session.clear()
            flash("Session expired. Please sign in again.", "warning")
            return redirect(url_for("login"))
        staff_type = session.get("staff_type", "picker")
        today      = date.today()
        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()

        if request.method == "POST" and not today_entry:
            try:
                picked           = max(0, int(request.form.get("picked",           0) or 0))
                missed           = max(0, int(request.form.get("missed",           0) or 0))
                sales_bills_open = max(0, int(request.form.get("sales_bills_open", 0) or 0))
                cs_sales_open    = max(0, int(request.form.get("cs_sales_open",    0) or 0))
                packing_done     = max(0, int(request.form.get("packing_done",     0) or 0))
                rack_organized   = 1 if request.form.get("rack_organized") == "1" else 0
                table_clean      = 1 if request.form.get("table_clean") == "1" else 0
                total_mins       = max(0, float(request.form.get("total_mins",     0) or 0))
                check_mins       = max(0, float(request.form.get("check_mins",     0) or 0))
                checked          = max(0, int(request.form.get("checked",          0) or 0))
                errors_found     = max(0, int(request.form.get("errors_found",     0) or 0))
                # Sanity caps
                if errors_found > checked:    errors_found = checked
                total_mins = min(total_mins, 1440)
                check_mins = min(check_mins, 1440)

                ne = KPIEntry(
                    emp_id           = emp_id,
                    sales_bills_open = sales_bills_open,
                    picked           = picked,
                    missed           = missed,
                    cs_sales_open    = cs_sales_open,
                    packing_done     = packing_done,
                    rack_organized   = rack_organized,
                    table_clean      = table_clean,
                    total_time       = round(total_mins / 60, 3),
                    checked          = checked,
                    errors_found     = errors_found,
                    check_time       = round(check_mins / 60, 3),
                    entry_date       = today
                )
                db.session.add(ne)
                db.session.commit()
                today_entry = ne
                total = picked + missed
                eff   = round(picked / total * 100, 1) if total > 0 else 0.0
                if eff < 85:
                    try:
                        socketio.emit("admin_alert", {
                            "name": session.get("user_name", "Unknown"),
                            "eff":  eff,
                            "type": staff_type
                        })
                    except Exception:
                        pass
                flash("Today's metrics recorded successfully.", "success")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Dashboard POST: {e}")
                flash("Could not save entry. Please try again.", "danger")
        elif request.method == "POST" and today_entry:
            flash("You have already submitted today's metrics.", "info")

        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        d_stats = build_analytics(get_period_entries(emp_id, "day"),   staff_type)
        w_stats = build_analytics(get_period_entries(emp_id, "week"),  staff_type)
        m_stats = build_analytics(get_period_entries(emp_id, "month"), staff_type)
        a_stats = build_analytics(all_entries, staff_type)

        lb      = build_leaderboard(staff_type)
        my_rank = next((i + 1 for i, x in enumerate(lb) if x["id"] == emp_id), "-")

        return render_template("dashboard.html",
            user_name=session.get("user_name", "User"), staff_type=staff_type,
            today=today, today_entry=today_entry,
            d_stats=d_stats, w_stats=w_stats, m_stats=m_stats, a_stats=a_stats,
            leaderboard=lb, my_rank=my_rank, recent=all_entries[:14])
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        db.session.rollback()
        # Do NOT redirect to login here — that causes an infinite redirect loop
        # when the session is valid but a DB or render error occurs.
        flash("Error loading dashboard. Please refresh the page.", "danger")
        return render_template("dashboard.html",
            user_name=session.get("user_name", "User"),
            staff_type=session.get("staff_type", "picker"),
            today=date.today(), today_entry=None,
            d_stats=None, w_stats=None, m_stats=None, a_stats=None,
            leaderboard=[], my_rank="-", recent=[]), 200


@app.route("/admin_dashboard")
@login_required
@admin_required
def admin_dashboard():
    try:
        employees = Employee.query.filter_by(is_admin=False).all()
        today     = date.today()
        rows      = []

        # Load all entries for all employees in ONE query (N+1 fix)
        emp_ids    = [emp.id for emp in employees]
        all_ents   = KPIEntry.query.filter(KPIEntry.emp_id.in_(emp_ids)).order_by(KPIEntry.entry_date.desc()).all()
        ents_by_id = {}
        for e in all_ents:
            ents_by_id.setdefault(e.emp_id, []).append(e)

        for emp in employees:
            try:
                ents  = ents_by_id.get(emp.id, [])
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
                logger.error(f"Admin row {emp.name}: {ex}")

        # Build leaderboard from already-loaded rows — avoids redundant DB queries
        def _to_lb(r):
            s = r["stats"]
            if not s:
                return None
            e = r["emp"]
            return {"id":e.id,"name":e.name,"email":e.email,"staff_type":e.staff_type,
                    "role":e.role,"score":s["eff_score"],"grade":s["grade"],
                    "pick_acc":s["pick_acc"],"pick_speed":s["pick_speed"],
                    "check_acc":s["check_acc"],"error_rate":s["error_rate"],
                    "ck_speed":s["ck_speed"],"consistency":s["consistency"],
                    "trend":s["trend"],"days":s["days"],"tp":s["tp"],"tm":s["tm"],
                    "pack_eff":s["pack_eff"],"workspace_score":s["workspace_score"],
                    "potential_eff":s["potential_eff"],"gap_items":s["gap_items"]}
        all_lb   = sorted([x for x in (_to_lb(r) for r in rows) if x],
                          key=lambda x: x["score"], reverse=True)
        pickers  = [r for r in all_lb if r["staff_type"] == "picker"]
        checkers = [r for r in all_lb if r["staff_type"] == "checker"]
        return render_template("admin.html", rows=rows,
                               pickers=pickers, checkers=checkers, all_lb=all_lb)
    except Exception as e:
        logger.error(f"Admin dashboard: {e}")
        db.session.rollback()
        flash("Error loading admin dashboard.", "danger")
        return render_template("admin.html", rows=[], pickers=[], checkers=[], all_lb=[]), 200


@app.route("/staff/<int:emp_id>")
@login_required
def staff_detail(emp_id):
    try:
        # Security: non-admin users can only view their own profile
        if not session.get("is_admin") and session.get("user_id") != emp_id:
            flash("You can only view your own profile.", "warning")
            return redirect(url_for("dashboard"))
        emp = db.session.get(Employee, emp_id)
        if not emp or emp.is_admin:
            flash("Employee not found.", "danger")
            return redirect(url_for("dashboard"))
        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        today   = date.today()
        d_ent   = [e for e in entries if e.entry_date == today]
        w_ent   = [e for e in entries if e.entry_date >= today - timedelta(days=6)]
        m_ent   = [e for e in entries if e.entry_date >= today - timedelta(days=29)]
        return render_template("staff_detail.html",
            emp=emp,
            a_stats=build_analytics(entries, emp.staff_type),
            d_stats=build_analytics(d_ent,   emp.staff_type),
            w_stats=build_analytics(w_ent,   emp.staff_type),
            m_stats=build_analytics(m_ent,   emp.staff_type),
            entries=entries[:20])
    except Exception as e:
        logger.error(f"Staff detail: {e}")
        flash("Could not load staff detail.", "danger")
        return redirect(url_for("dashboard"))


@app.route("/admin/staff/<int:emp_id>")
@login_required
@admin_required
def admin_staff_detail(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp or emp.is_admin:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        today   = date.today()
        d_ent   = [e for e in entries if e.entry_date == today]
        w_ent   = [e for e in entries if e.entry_date >= today - timedelta(days=6)]
        m_ent   = [e for e in entries if e.entry_date >= today - timedelta(days=29)]
        return render_template("staff_detail.html",
            emp=emp,
            a_stats=build_analytics(entries, emp.staff_type),
            d_stats=build_analytics(d_ent,   emp.staff_type),
            w_stats=build_analytics(w_ent,   emp.staff_type),
            m_stats=build_analytics(m_ent,   emp.staff_type),
            entries=entries[:20],
            is_admin_view=True)
    except Exception as e:
        logger.error(f"Admin staff detail: {e}")
        flash("Could not load staff detail.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/export_data")
@login_required
@admin_required
def export_data():
    """CSV export — quick management download."""
    try:
        entries = KPIEntry.query.join(Employee).add_columns(
            Employee.name, Employee.email
        ).order_by(KPIEntry.entry_date.desc()).all()
        def generate():
            yield 'Employee_ID,Name,Date,Sales_Bills_Open,Picked,Missed,CS_Sales_Open,Packing_Done,Rack_Organized,Table_Clean,Total_Time_hrs,Accuracy,Pick_Speed\n'
            for row in entries:
                e    = row[0]
                name = row.name.replace(",", " ")
                yield (f"{e.emp_id},{name},{e.entry_date},{e.effective_bills},"
                       f"{e.picked},{e.missed},{e.effective_cs},{e.packing_done or 0},"
                       f"{e.rack_organized or 0},{e.table_clean or 0},{e.effective_time},"
                       f"{e.accuracy}%,{e.pick_speed}\n")
        return Response(generate(), mimetype='text/csv',
                        headers={"Content-Disposition": "attachment; filename=pharma_kpi.csv"})
    except Exception as e:
        logger.error(f"Export error: {e}")
        flash("Could not generate export.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/download/<int:emp_id>")
@login_required
@admin_required
def download_report(emp_id):
    try:
        from utils import generate_visual_pdf
        emp  = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        ents = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.asc()).all()
        if not ents:
            flash("No data for this employee.", "warning")
            return redirect(url_for("admin_dashboard"))
        today = date.today()
        d_ent = [e for e in ents if e.entry_date == today]
        w_ent = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
        m_ent = [e for e in ents if e.entry_date >= today - timedelta(days=29)]
        payload = dict(
            all_entries = ents,
            staff_type  = emp.staff_type,
            all_stats   = build_analytics(ents,  emp.staff_type),
            day_stats   = build_analytics(d_ent, emp.staff_type),
            week_stats  = build_analytics(w_ent, emp.staff_type),
            month_stats = build_analytics(m_ent, emp.staff_type),
        )
        buf = generate_visual_pdf(emp.name, payload)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"KRA_{emp.name.replace(' ', '_')}.pdf")
    except Exception as e:
        logger.error(f"Download error: {e}")
        flash("Could not generate PDF. Please try again.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/bulk_zip")
@login_required
@admin_required
def bulk_zip():
    try:
        from utils import generate_visual_pdf
        employees = Employee.query.filter_by(is_admin=False).all()
        zbuf      = io.BytesIO()
        today     = date.today()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            for emp in employees:
                try:
                    ents = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.asc()).all()
                    if not ents:
                        continue
                    d_ent = [e for e in ents if e.entry_date == today]
                    w_ent = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
                    m_ent = [e for e in ents if e.entry_date >= today - timedelta(days=29)]
                    payload = dict(
                        all_entries = ents, staff_type  = emp.staff_type,
                        all_stats   = build_analytics(ents,  emp.staff_type),
                        day_stats   = build_analytics(d_ent, emp.staff_type),
                        week_stats  = build_analytics(w_ent, emp.staff_type),
                        month_stats = build_analytics(m_ent, emp.staff_type)
                    )
                    pdf = generate_visual_pdf(emp.name, payload)
                    zf.writestr(f"KRA_{emp.name.replace(' ', '_')}.pdf", pdf.read())
                except Exception as ex:
                    logger.error(f"Bulk zip {emp.name}: {ex}")
        zbuf.seek(0)
        return send_file(zbuf, mimetype="application/zip", as_attachment=True,
                         download_name="All_KRA_Reports.zip")
    except Exception as e:
        logger.error(f"Bulk zip error: {e}")
        flash("Could not generate bulk export.", "danger")
        return redirect(url_for("admin_dashboard"))


# ══════════════════════════════════════════════════
#  8b. ADMIN USER MANAGEMENT ROUTES
# ══════════════════════════════════════════════════
@app.route("/admin/add_user", methods=["POST"])
@login_required
@admin_required
def admin_add_user():
    try:
        # Auto-capitalize: title-case the name
        name       = " ".join(w.capitalize() for w in request.form.get("name", "").strip().split())
        email      = request.form.get("email", "").lower().strip()
        password   = request.form.get("password", "")
        staff_type = request.form.get("staff_type", "picker").strip()

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("admin_dashboard"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for("admin_dashboard"))
        if staff_type not in ("picker", "checker"):
            flash("Invalid staff type.", "danger")
            return redirect(url_for("admin_dashboard"))
        # Enforce @pharmaip.com domain
        if not email.endswith("@pharmaip.com"):
            flash("Email must end with @pharmaip.com.", "danger")
            return redirect(url_for("admin_dashboard"))
        if Employee.query.filter_by(email=email).first():
            flash(f"Email '{email}' is already registered.", "warning")
            return redirect(url_for("admin_dashboard"))

        emp = Employee(
            name=name, email=email,
            staff_type=staff_type, is_admin=False,
            role=f"Operations {staff_type.title()}"
        )
        emp.set_password(password)
        db.session.add(emp)
        db.session.commit()
        flash(f"✅ '{name}' added successfully. They can now log in.", "success")
        logger.info(f"Admin created user: {email}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_add_user error: {e}")
        flash("Could not add staff member. Please try again.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_user/<int:emp_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_user(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        if emp.is_admin:
            flash("Cannot delete admin accounts.", "danger")
            return redirect(url_for("admin_dashboard"))
        name = emp.name
        db.session.delete(emp)   # cascade deletes all KPIEntry rows too
        db.session.commit()
        flash(f"🗑️ '{name}' and all their data have been removed.", "success")
        logger.info(f"Admin deleted user id={emp_id} name={name}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_delete_user error: {e}")
        flash("Could not delete employee. Please try again.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_password/<int:emp_id>", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        if emp.is_admin:
            flash("Cannot reset admin password via this form.", "danger")
            return redirect(url_for("admin_dashboard"))
        new_pw = request.form.get("new_password", "")
        if len(new_pw) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return redirect(url_for("admin_dashboard"))
        emp.set_password(new_pw)
        db.session.commit()
        flash(f"🔑 Password reset for '{emp.name}' successfully.", "success")
        logger.info(f"Admin reset password for id={emp_id}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_reset_password error: {e}")
        flash("Could not reset password. Please try again.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/update_user/<int:emp_id>", methods=["POST"])
@login_required
@admin_required
def admin_update_user(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        if emp.is_admin:
            flash("Cannot edit admin accounts.", "danger")
            return redirect(url_for("admin_dashboard"))

        # Auto-capitalize name
        new_name  = " ".join(w.capitalize() for w in request.form.get("name", "").strip().split())
        new_email = request.form.get("email", "").lower().strip()
        new_type  = request.form.get("staff_type", "").strip()

        if not new_name or not new_email:
            flash("Name and email are required.", "danger")
            return redirect(url_for("admin_dashboard"))
        if new_type not in ("picker", "checker"):
            flash("Invalid staff type.", "danger")
            return redirect(url_for("admin_dashboard"))
        if not new_email.endswith("@pharmaip.com"):
            flash("Email must end with @pharmaip.com.", "danger")
            return redirect(url_for("admin_dashboard"))

        # Check email uniqueness — allow keeping their own email
        existing = Employee.query.filter_by(email=new_email).first()
        if existing and existing.id != emp_id:
            flash(f"Email '{new_email}' is already used by another employee.", "warning")
            return redirect(url_for("admin_dashboard"))

        emp.name       = new_name
        emp.email      = new_email
        emp.staff_type = new_type
        emp.role       = f"Operations {new_type.title()}"
        db.session.commit()
        flash(f"✏️ '{new_name}' updated successfully.", "success")
        logger.info(f"Admin updated user id={emp_id} email={new_email}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_update_user error: {e}")
        flash("Could not update employee. Please try again.", "danger")
    return redirect(url_for("admin_dashboard"))


# ══════════════════════════════════════════════════
#  9. ERROR HANDLERS (from File 2)
# ══════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    # Avoid redirect loops: if the destination itself 404s, go to login
    if session.get("user_id"):
        target = "admin_dashboard" if session.get("is_admin") else "dashboard"
        # Don't redirect back to the same path that 404'd
        if request.path not in (url_for("dashboard"), url_for("admin_dashboard")):
            try:
                return redirect(url_for(target))
            except Exception:
                pass
    session.clear()
    return redirect(url_for("login"))


@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 error: {e}")
    try:
        db.session.rollback()
    except Exception:
        pass
    session.clear()
    # Redirect instead of render to avoid template errors compounding the 500
    return redirect(url_for("login"))


# ══════════════════════════════════════════════════
#  10. ENTRY POINT
# ══════════════════════════════════════════════════
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))