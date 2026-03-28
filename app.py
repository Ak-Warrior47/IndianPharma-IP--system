import os, logging, zipfile, io, atexit, json
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
app.secret_key = os.environ.get("SECRET_KEY", "ip_pharma_v6_secure")

db_url = os.environ.get("DATABASE_URL", "sqlite:///pharma_v6.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


# ── MODELS ──────────────────────────────────────────
class Employee(db.Model):
    __tablename__ = "employees"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    staff_type    = db.Column(db.String(20), default="picker")
    role          = db.Column(db.String(100), default="Operations Specialist")
    is_admin      = db.Column(db.Boolean, default=False)
    entries       = db.relationship("KPIEntry", backref="owner", lazy="select", cascade="all, delete-orphan")

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class KPIEntry(db.Model):
    __tablename__  = "kpi_entries"
    id             = db.Column(db.Integer, primary_key=True)
    emp_id         = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    bills          = db.Column(db.Integer, default=0)
    picked         = db.Column(db.Integer, default=0)
    missed         = db.Column(db.Integer, default=0)
    boxes          = db.Column(db.Integer, default=0)
    sweep          = db.Column(db.Float, default=0.0)
    checked        = db.Column(db.Integer, default=0)
    errors_found   = db.Column(db.Integer, default=0)
    check_time     = db.Column(db.Float, default=0.0)
    entry_date     = db.Column(db.Date, nullable=False, index=True)
    report_sent    = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint("emp_id", "entry_date", name="_emp_date_uc"),)

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


# ── DECORATORS ──────────────────────────────────────
def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if "user_id" not in session:
            flash("Please sign in.", "warning")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get("is_admin"):
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return d


# ── ANALYTICS ───────────────────────────────────────
def safe_div(a, b, default=0.0):
    try: return a / b if b else default
    except: return default

def build_analytics(entries, staff_type="picker"):
    """
    POTENTIAL FORMULA:
    Picker:
      - Benchmark: 98% accuracy, 200 items/hr
      - Efficiency = (accuracy/100)*60 + min(speed/200,1)*40
      - Potential_items = 200 * total_sweep_hours
      - Potential_eff   = current_eff + (98-acc)*0.5 + (200-speed)*0.1  capped at 100
      - Gap_items       = potential_items - actual_items
    Checker:
      - Benchmark: 100% error detection, 150 checks/hr
      - Efficiency = (check_acc/100)*70 + min(ck_speed/150,1)*30
      - Potential_items = 150 * total_check_hours
      - Potential_eff   = current_eff + (100-check_acc)*0.5  capped at 100
    """
    try:
        if not entries: return None
        tp  = sum(int(e.picked or 0) for e in entries)
        tm  = sum(int(e.missed or 0) for e in entries)
        ti  = tp + tm
        tb  = sum(int(e.bills or 0)  for e in entries)
        tbx = sum(int(e.boxes or 0)  for e in entries)
        ts  = round(sum(float(e.sweep or 0) for e in entries), 3)
        tck = sum(int(e.checked or 0) for e in entries)
        ter = sum(int(e.errors_found or 0) for e in entries)
        tct = round(sum(float(e.check_time or 0) for e in entries), 3)

        pick_acc   = round(safe_div(tp, ti) * 100, 1)
        pick_speed = round(safe_div(ti, ts), 1)
        check_acc  = round(safe_div(ter, tck) * 100, 1)
        ck_speed   = round(safe_div(tck, tct), 1)

        if staff_type == "checker":
            eff_score = round(min(100, (check_acc / 100 * 70) + (min(safe_div(ck_speed, 150), 1) * 30)), 1)
            potential_items = int(150.0 * tct) if tct > 0 else tck
            potential_eff   = round(min(100, eff_score + max(0, (100 - check_acc) * 0.5)), 1)
            gap_items       = max(0, potential_items - tck)
            acc_for_grade   = check_acc
        else:
            eff_score = round(min(100, (pick_acc / 100 * 60) + (min(safe_div(pick_speed, 200), 1) * 40)), 1)
            potential_items = int(200.0 * ts)
            potential_eff   = round(min(100, eff_score + max(0, (98 - pick_acc) * 0.5 + (200 - pick_speed) * 0.1)), 1)
            gap_items       = max(0, potential_items - ti)
            acc_for_grade   = pick_acc

        if staff_type == "picker":
            if pick_acc >= 98 and tbx >= 15: grade, fb = "ELITE",        "Exceptional pick accuracy & CS handling. Gold Standard."
            elif pick_acc >= 95:             grade, fb = "PROFICIENT",   "Meets standard pharma pick accuracy requirements."
            elif pick_acc >= 88:             grade, fb = "SATISFACTORY", "Acceptable. Focus on reducing missed picks."
            else:                            grade, fb = "RE-TRAINING",  "Pick accuracy below safety threshold. Intervention required."
        else:
            if check_acc >= 97 and tbx >= 10: grade, fb = "ELITE",        "Exceptional verification accuracy. Zero-error standard met."
            elif check_acc >= 94:             grade, fb = "PROFICIENT",   "Good check accuracy. Minor improvements remain."
            elif check_acc >= 87:             grade, fb = "SATISFACTORY", "Acceptable check rate. Increase error detection."
            else:                             grade, fb = "RE-TRAINING",  "Verification accuracy below threshold. Re-training needed."

        # Consistency score — stddev of daily accuracies
        daily_accs = [e.accuracy for e in entries if (e.picked+e.missed) > 0]
        if len(daily_accs) > 1:
            mean_a = sum(daily_accs) / len(daily_accs)
            variance = sum((x - mean_a)**2 for x in daily_accs) / len(daily_accs)
            consistency = round(max(0, 100 - (variance**0.5) * 2), 1)
        else:
            consistency = 100.0 if daily_accs else 0.0

        trend = "stable"
        if len(entries) >= 4:
            recent_avg = sum(e.accuracy for e in entries[:2]) / 2
            older_avg  = sum(e.accuracy for e in entries[-2:]) / 2
            if recent_avg > older_avg + 2:   trend = "improving"
            elif recent_avg < older_avg - 2: trend = "declining"

        return dict(
            tp=tp, tm=tm, ti=ti, tb=tb, tbx=tbx, ts=ts,
            tck=tck, ter=ter, tct=tct,
            pick_acc=pick_acc, pick_speed=pick_speed,
            check_acc=check_acc, ck_speed=ck_speed,
            eff_score=eff_score, grade=grade, feedback=fb,
            potential_items=potential_items, potential_eff=potential_eff,
            gap_items=gap_items, consistency=consistency, trend=trend,
            days=len(entries)
        )
    except Exception as e:
        logger.error(f"build_analytics error: {e}")
        return None

def get_period_entries(emp_id, period):
    today = date.today()
    starts = {"day": today, "week": today-timedelta(days=6), "month": today-timedelta(days=29)}
    start  = starts.get(period, today)
    try:
        return KPIEntry.query.filter(
            KPIEntry.emp_id == emp_id,
            KPIEntry.entry_date >= start,
            KPIEntry.entry_date <= today
        ).order_by(KPIEntry.entry_date.desc()).all()
    except Exception as e:
        logger.error(f"get_period_entries: {e}")
        return []

def build_leaderboard(staff_type=None):
    """Build full leaderboard for all registered staff, optionally filtered by type."""
    try:
        query = Employee.query.filter_by(is_admin=False)
        if staff_type:
            query = query.filter_by(staff_type=staff_type)
        employees = query.all()
        lb = []
        for emp in employees:
            entries = KPIEntry.query.filter_by(emp_id=emp.id).all()
            stats = build_analytics(entries, emp.staff_type)
            if not stats: continue
            lb.append({
                "id": emp.id, "name": emp.name, "email": emp.email,
                "staff_type": emp.staff_type, "role": emp.role,
                "score": stats["eff_score"], "grade": stats["grade"],
                "pick_acc": stats["pick_acc"], "pick_speed": stats["pick_speed"],
                "check_acc": stats["check_acc"], "ck_speed": stats["ck_speed"],
                "consistency": stats["consistency"], "trend": stats["trend"],
                "days": stats["days"], "tp": stats["tp"], "tm": stats["tm"],
                "potential_eff": stats["potential_eff"], "gap_items": stats["gap_items"],
            })
        lb.sort(key=lambda x: x["score"], reverse=True)
        return lb
    except Exception as e:
        logger.error(f"build_leaderboard: {e}")
        return []


# ── SCHEDULER ───────────────────────────────────────
def start_scheduler():
    def job():
        with app.app_context():
            try:
                target  = date.today() - timedelta(days=2)
                pending = KPIEntry.query.filter_by(entry_date=target, report_sent=False).all()
                for e in pending:
                    emp = db.session.get(Employee, e.emp_id)
                    if emp: logger.info(f"[48HR] {emp.name} — {target}")
                    e.report_sent = True
                db.session.commit()
            except Exception as err:
                logger.error(f"Scheduler error: {err}")
    try:
        s = BackgroundScheduler(daemon=True)
        s.add_job(job, "interval", hours=24, id="auto_report", replace_existing=True)
        s.start()
        atexit.register(lambda: s.shutdown(wait=False))
    except Exception as e:
        logger.warning(f"Scheduler failed to start: {e}")


# ── DB INIT ─────────────────────────────────────────
def init_db():
    with app.app_context():
        try:
            db.create_all()
            def make(name, email, pw, stype="picker", admin=False):
                ex = Employee.query.filter_by(email=email).first()
                if ex: return ex
                emp = Employee(name=name, email=email, staff_type=stype, is_admin=admin,
                               role="Admin" if admin else f"Operations {stype.title()}")
                emp.set_password(pw)
                db.session.add(emp); db.session.flush(); return emp

            make("System Admin",  "admin@pharmaip.com",  "admin123", admin=True)
            p1 = make("Rahul Sharma", "rahul@pharmaip.com", "test1234", stype="picker")
            if p1 and not KPIEntry.query.filter_by(emp_id=p1.id).first():
                for d,b,pk,ms,bx,sw in [(6,40,195,5,10,1.5),(5,42,210,2,12,1.4),(4,38,188,8,9,1.6),
                    (3,45,220,1,14,1.3),(2,41,200,4,11,1.5),(1,44,215,3,13,1.4),(0,46,225,2,15,1.3)]:
                    db.session.add(KPIEntry(emp_id=p1.id,bills=b,picked=pk,missed=ms,boxes=bx,sweep=sw,
                        entry_date=date.today()-timedelta(days=d)))
            c1 = make("Priya Patel", "priya@pharmaip.com", "test1234", stype="checker")
            if c1 and not KPIEntry.query.filter_by(emp_id=c1.id).first():
                for d,b,pk,ms,bx,sw,ck,er,cm in [(6,30,160,18,8,2.0,178,14,90),(5,28,172,12,7,1.8,184,10,85),
                    (4,33,190,6,10,1.6,196,6,80),(3,29,168,14,6,1.9,182,12,88),(2,35,195,9,11,1.5,204,8,82),
                    (1,31,180,10,9,1.7,190,9,86),(0,36,200,7,12,1.4,207,7,78)]:
                    db.session.add(KPIEntry(emp_id=c1.id,bills=b,picked=pk,missed=ms,boxes=bx,sweep=sw,
                        checked=ck,errors_found=er,check_time=round(cm/60,3),
                        entry_date=date.today()-timedelta(days=d)))
            db.session.commit()
            logger.info("DB init complete")
        except Exception as e:
            logger.error(f"DB init error: {e}")
            db.session.rollback()

init_db()
start_scheduler()


# ── ROUTES ──────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("admin_dashboard" if session.get("is_admin") else "dashboard"))
    if request.method == "POST":
        try:
            email       = request.form.get("email","").lower().strip()
            password    = request.form.get("password","")
            role_choice = request.form.get("staff_type","").strip()
            user = Employee.query.filter_by(email=email).first()
            if not user:
                flash("No account found with that email.", "danger")
                return render_template("login.html")
            if not user.check_password(password):
                flash("Incorrect password.", "danger")
                return render_template("login.html")
            if not user.is_admin and role_choice in ("picker","checker"):
                user.staff_type = role_choice
                db.session.commit()
            session.permanent = True
            session.update({"user_id":user.id,"user_name":user.name,
                            "staff_type":user.staff_type,"is_admin":user.is_admin})
            return redirect(url_for("admin_dashboard" if user.is_admin else "dashboard"))
        except Exception as e:
            logger.error(f"Login error: {e}")
            flash("System error. Please try again.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET","POST"])
@login_required
def dashboard():
    try:
        emp_id     = session["user_id"]
        staff_type = session.get("staff_type","picker")
        today      = date.today()
        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()

        if request.method == "POST" and not today_entry:
            try:
                picked = max(0, int(request.form.get("picked",0) or 0))
                missed = max(0, int(request.form.get("missed",0) or 0))
                sweep_mins = max(0, float(request.form.get("sweep_mins",0) or 0))
                check_mins = max(0, float(request.form.get("check_mins",0) or 0))
                ne = KPIEntry(
                    emp_id=emp_id,
                    bills=max(0, int(request.form.get("bills",0) or 0)),
                    picked=picked, missed=missed,
                    boxes=max(0, int(request.form.get("boxes",0) or 0)),
                    sweep=round(sweep_mins/60, 3),
                    checked=max(0, int(request.form.get("checked",0) or 0)),
                    errors_found=max(0, int(request.form.get("errors_found",0) or 0)),
                    check_time=round(check_mins/60, 3),
                    entry_date=today
                )
                db.session.add(ne); db.session.commit()
                today_entry = ne
                total = picked + missed
                eff   = round(picked/total*100,1) if total > 0 else 0.0
                if eff < 85:
                    try: socketio.emit("admin_alert",{"name":session["user_name"],"eff":eff,"type":staff_type})
                    except: pass
                flash("Today's metrics recorded successfully.", "success")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Dashboard POST: {e}")
                flash("Could not save entry. Please try again.", "danger")

        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        d_stats = build_analytics(get_period_entries(emp_id,"day"),   staff_type)
        w_stats = build_analytics(get_period_entries(emp_id,"week"),  staff_type)
        m_stats = build_analytics(get_period_entries(emp_id,"month"), staff_type)
        a_stats = build_analytics(all_entries, staff_type)

        lb      = build_leaderboard(staff_type)
        my_rank = next((i+1 for i,x in enumerate(lb) if x["id"]==emp_id), "-")

        return render_template("dashboard.html",
            user_name=session["user_name"], staff_type=staff_type,
            today=today, today_entry=today_entry,
            d_stats=d_stats, w_stats=w_stats, m_stats=m_stats, a_stats=a_stats,
            leaderboard=lb, my_rank=my_rank, recent=all_entries[:14])
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return redirect(url_for("login"))


@app.route("/staff/<int:emp_id>")
@login_required
def staff_detail(emp_id):
    """Staff detail page — shown when clicking a leaderboard entry."""
    try:
        emp     = db.session.get(Employee, emp_id)
        if not emp or emp.is_admin:
            flash("Employee not found.", "danger")
            return redirect(url_for("dashboard"))
        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        today   = date.today()
        d_ent   = [e for e in entries if e.entry_date == today]
        w_ent   = [e for e in entries if e.entry_date >= today-timedelta(days=6)]
        m_ent   = [e for e in entries if e.entry_date >= today-timedelta(days=29)]
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


@app.route("/admin")
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
                w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
                m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
                rows.append(dict(emp=emp,
                    stats  =build_analytics(ents,  emp.staff_type),
                    d_stats=build_analytics(d_ent, emp.staff_type),
                    w_stats=build_analytics(w_ent, emp.staff_type),
                    m_stats=build_analytics(m_ent, emp.staff_type),
                    count=len(ents)))
            except Exception as ex:
                logger.error(f"Admin row {emp.name}: {ex}")
        all_lb   = build_leaderboard()
        pickers  = [r for r in all_lb if r["staff_type"]=="picker"]
        checkers = [r for r in all_lb if r["staff_type"]=="checker"]
        return render_template("admin.html", rows=rows,
                               pickers=pickers, checkers=checkers, all_lb=all_lb)
    except Exception as e:
        logger.error(f"Admin dashboard: {e}")
        flash("Error loading admin dashboard.", "danger")
        return redirect(url_for("login"))


@app.route("/admin/staff/<int:emp_id>")
@login_required
@admin_required
def admin_staff_detail(emp_id):
    """Admin view of staff detail with download button."""
    try:
        emp     = db.session.get(Employee, emp_id)
        if not emp or emp.is_admin:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        today   = date.today()
        d_ent   = [e for e in entries if e.entry_date == today]
        w_ent   = [e for e in entries if e.entry_date >= today-timedelta(days=6)]
        m_ent   = [e for e in entries if e.entry_date >= today-timedelta(days=29)]
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


@app.route("/download/<int:emp_id>")
@login_required
@admin_required
def download_report(emp_id):
    try:
        from utils import generate_visual_pdf
        emp   = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        ents  = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.asc()).all()
        if not ents:
            flash("No data for this employee.", "warning")
            return redirect(url_for("admin_dashboard"))
        today = date.today()
        d_ent = [e for e in ents if e.entry_date == today]
        w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
        m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
        payload = dict(all_entries=ents, staff_type=emp.staff_type,
            all_stats  =build_analytics(ents,  emp.staff_type),
            day_stats  =build_analytics(d_ent, emp.staff_type),
            week_stats =build_analytics(w_ent, emp.staff_type),
            month_stats=build_analytics(m_ent, emp.staff_type))
        buf = generate_visual_pdf(emp.name, payload)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"KRA_{emp.name.replace(' ','_')}.pdf")
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
        with zipfile.ZipFile(zbuf,"w",zipfile.ZIP_DEFLATED) as zf:
            for emp in employees:
                try:
                    ents = KPIEntry.query.filter_by(emp_id=emp.id).order_by(KPIEntry.entry_date.asc()).all()
                    if not ents: continue
                    d_ent = [e for e in ents if e.entry_date == today]
                    w_ent = [e for e in ents if e.entry_date >= today-timedelta(days=6)]
                    m_ent = [e for e in ents if e.entry_date >= today-timedelta(days=29)]
                    payload = dict(all_entries=ents,staff_type=emp.staff_type,
                        all_stats=build_analytics(ents,emp.staff_type),
                        day_stats=build_analytics(d_ent,emp.staff_type),
                        week_stats=build_analytics(w_ent,emp.staff_type),
                        month_stats=build_analytics(m_ent,emp.staff_type))
                    pdf = generate_visual_pdf(emp.name, payload)
                    zf.writestr(f"KRA_{emp.name.replace(' ','_')}.pdf", pdf.read())
                except Exception as ex:
                    logger.error(f"Bulk zip {emp.name}: {ex}")
        zbuf.seek(0)
        return send_file(zbuf, mimetype="application/zip", as_attachment=True,
                         download_name="All_KRA_Reports.zip")
    except Exception as e:
        logger.error(f"Bulk zip error: {e}")
        flash("Could not generate bulk export.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/health")
def health():
    return jsonify(status="Pharma IP v6 Operational"), 200

@app.errorhandler(404)
def not_found(e):
    return redirect(url_for("login"))

@app.errorhandler(500)
def server_error(e):
    logger.error(f"500: {e}")
    db.session.rollback()
    flash("An internal error occurred. Please try again.", "danger")
    return redirect(url_for("login"))

if __name__ == "__main__":
    socketio.run(app, debug=False, port=int(os.environ.get("PORT",5000)))
