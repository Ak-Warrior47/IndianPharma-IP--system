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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
IS_PRODUCTION = os.environ.get("RENDER") or os.environ.get("DATABASE_URL")

app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "pharma_secure_key_2024"),
    SESSION_COOKIE_SECURE=bool(IS_PRODUCTION),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=False,
    PREFERRED_URL_SCHEME='https',
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300}
)

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    logger.warning("⚠️ DATABASE_URL not set! Using SQLite (local development mode)")
    db_url = "sqlite:///pharma_final.db"
elif db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
logger.info(f"Using database: {db_url.split('@')[0] if '@' in db_url else 'SQLite (local)'}")

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


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
    rack_organized   = db.Column(db.Integer, default=0)   # 0-10 score
    table_clean      = db.Column(db.Integer, default=0)   # 0=No 1=Yes
    sweep_done       = db.Column(db.Integer, default=0)   # 0=No 1=Yes
    total_time       = db.Column(db.Float, default=0.0)
    checked          = db.Column(db.Integer, default=0)
    errors_found     = db.Column(db.Integer, default=0)
    check_time       = db.Column(db.Float, default=0.0)
    bills            = db.Column(db.Integer, default=0)
    boxes            = db.Column(db.Integer, default=0)
    sweep            = db.Column(db.Float, default=0.0)
    entry_date       = db.Column(db.Date, nullable=False, index=True)
    report_sent      = db.Column(db.Boolean, default=False)
    bills_received   = db.Column(db.Integer, default=0)  # Checker: total bills received (manual)
    pending_bills_manual = db.Column(db.Integer, default=0)  # Checker: manually entered pending bills
    total_bills_received = db.Column(db.Integer, default=0)  # Picker: total bills given to pick

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
    def effective_bills(self):
        return self.sales_bills_open or self.bills or 0

    @property
    def effective_time(self):
        return self.total_time or self.sweep or 0

    @property
    def _sales_bill_effective(self):
        return self.sales_bills_open or self.bills or 0

    @property
    def _total_time_hrs(self):
        return self.total_time or self.sweep or 0

    @property
    def check_rate(self):
        """Normal rate = checked / (checked + errors_found) × 100"""
        total = (self.checked or 0) + (self.errors_found or 0)
        return round((self.checked or 0) / total * 100, 1) if total > 0 else 0.0

    @property
    def daily_speed(self):
        """Check speed for this single entry: (checked + errors_found) / 9hrs"""
        total = (self.checked or 0) + (self.errors_found or 0)
        return round(total / 9.0, 1)

    @property
    def pick_speed(self):
        """For pickers: items/hr. For checkers: items checked/check_time hr."""
        t = (self.picked or 0) + (self.missed or 0)
        tt = self.total_time or self.sweep or 0
        return round(t / max(tt, 0.001), 1) if tt > 0 else 0


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    action = db.Column(db.String(100))
    target = db.Column(db.String(100))
    details = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)



class Complaint(db.Model):
    """Admin-entered post-delivery complaints — minus marking system."""
    __tablename__ = "complaints"
    id            = db.Column(db.Integer, primary_key=True)
    entry_date    = db.Column(db.Date, nullable=False, default=date.today, index=True)
    reported_by   = db.Column(db.Integer, db.ForeignKey("employees.id"))  # admin
    # Optional: specific staff this complaint is pinned to. If NULL, applies to whole role.
    target_emp_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True, index=True)
    description   = db.Column(db.Text, nullable=False)
    complaint_type= db.Column(db.String(50), default="delivery")  # delivery/quality/missing/damage
    # Suggested deductions per role (admin can override)
    picker_deduct  = db.Column(db.Float, default=0.0)
    checker_deduct = db.Column(db.Float, default=0.0)
    purchaser_deduct = db.Column(db.Float, default=0.0)
    # Final deductions entered by admin
    picker_final   = db.Column(db.Float, default=0.0)
    checker_final  = db.Column(db.Float, default=0.0)
    purchaser_final= db.Column(db.Float, default=0.0)
    # Status
    is_resolved    = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

class PastEntryWindow(db.Model):
    """Admin-opened windows that allow staff to submit data for a past date."""
    __tablename__ = "past_entry_windows"
    id          = db.Column(db.Integer, primary_key=True)
    past_date   = db.Column(db.Date, nullable=False, unique=True)   # the date being opened
    opened_by   = db.Column(db.Integer, db.ForeignKey("employees.id"))
    opened_at   = db.Column(db.DateTime, default=datetime.utcnow)
    is_active   = db.Column(db.Boolean, default=True)               # admin can close it again


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def safe_div(a, b, default=0.0):
    try: return a / b if b else default
    except Exception: return default


def grade_from_score(score):
    if score >= 90:   return "EXCELLENT"
    elif score >= 75: return "GOOD"
    elif score >= 60: return "AVERAGE"
    else:             return "NEEDS WORK"




def get_complaint_deduction(staff_type: str, emp_id: int = None) -> float:
    """Get total pending complaint deductions for a staff member.

    - Complaints with target_emp_id = this employee -> always apply.
    - Complaints with target_emp_id = NULL (unpinned) -> apply to whole role (legacy).
    """
    try:
        q = Complaint.query.filter_by(is_resolved=False)
        complaints = q.all()
        total = 0.0
        for c in complaints:
            # Skip if pinned to someone else
            if c.target_emp_id is not None and emp_id is not None and c.target_emp_id != emp_id:
                continue
            if staff_type == "picker":
                total += c.picker_final or 0
            elif staff_type == "checker":
                total += c.checker_final or 0
            elif staff_type == "purchaser":
                total += c.purchaser_final or 0
        return min(total, 30.0)  # cap total deduction at 30 pts
    except Exception:
        return 0.0

def build_analytics(entries: List[KPIEntry], staff_type: str = "picker", emp_id: int = None) -> Optional[Dict[str, Any]]:
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
        tct  = sum(float(e.check_time or 0) for e in entries)
        days = len(entries)
        # Checker: bills received + pending (both manual entry)
        tbr  = sum(int(e.bills_received or 0) for e in entries) if staff_type == "checker" else 0
        tpbm = sum(int(e.pending_bills_manual or 0) for e in entries) if staff_type == "checker" else 0
        # Picker: total bills received to pick
        tbr_picker = sum(int(e.total_bills_received or 0) for e in entries) if staff_type == "picker" else 0

        FIXED_HRS   = 9.0
        ts_total    = FIXED_HRS * days
        tck_normal  = tck
        tck_urgent  = ter
        tck_total   = tck_normal + tck_urgent
        tsb_normal  = tsb
        tsb_urgent  = sum(int(e.cs_sales_open or 0) for e in entries) if staff_type == "checker" else 0
        tsb_total   = tsb_normal + tsb_urgent

        # Checker: clearance = SB Checked / (SB Checked + Pending Manual)
        tsb_checked_total = tsb_normal + tsb_urgent if staff_type == "checker" else 0
        pending_bills  = tpbm  # now manually entered by checker
        total_for_clearance = tsb_checked_total + pending_bills
        clearance_rate = round(safe_div(tsb_checked_total, total_for_clearance) * 100, 1) if total_for_clearance > 0 else 100.0
        # Picker: bill fulfilment = SB Picked / Total Bills Received
        bill_fulfilment = round(min(safe_div(tsb_normal, tbr_picker) * 100, 100.0), 1) if tbr_picker > 0 else 0.0

        pick_acc        = round(safe_div(tp, ti) * 100, 1)
        pick_speed      = round(safe_div(ti, ts_total), 1)
        packing_eff     = round(safe_div(tpk, tsb_normal) * 100, 1) if tsb_normal > 0 else 0.0
        cs_fulfilment   = round(min(safe_div(tpk, tcs) * 100, 100.0), 1) if tcs > 0 else 0.0
        # Workspace: rack(0-10)×5pts + table_clean(0/1)×3pts + sweep(0/1)×2pts = 10pts max/day
        tsd             = sum(int(e.sweep_done or 0) for e in entries)  # total sweep days
        rack_ws         = safe_div(tro, 10.0 * days) * 5   # rack score 0-5
        table_ws        = safe_div(ttc, days) * 3           # table yes/no 0-3
        sweep_ws        = safe_div(tsd, days) * 2           # sweep yes/no 0-2
        workspace_score = round((rack_ws + table_ws + sweep_ws) / 10 * 100, 1) if days > 0 else 0.0

        check_speed = round(safe_div(tck_total, ts_total), 1)
        normal_pct  = round(safe_div(tck_normal, tck_total) * 100, 1) if tck_total > 0 else 0.0
        urgent_pct  = round(safe_div(tck_urgent, tck_total) * 100, 1) if tck_total > 0 else 0.0
        check_acc   = round(min(safe_div(check_speed, 25.0), 1.0) * 100, 1)
        error_rate  = urgent_pct
        ck_speed    = check_speed

        if staff_type == "checker":
            potential_items = int(25.0 * ts_total)
            gap_items       = max(potential_items - tck_total, 0)
            potential_eff   = round(safe_div(tck_total, max(potential_items, 1)) * 100, 1)
        else:
            potential_items = int(200.0 * ts_total)
            gap_items       = max(potential_items - ti, 0)
            potential_eff   = round(safe_div(ti, max(potential_items, 1)) * 100, 1)

        if staff_type == "checker":
            daily_rates = [((e.checked or 0)+(e.errors_found or 0))/FIXED_HRS
                           for e in entries if ((e.checked or 0)+(e.errors_found or 0))>0]
        else:
            daily_rates = [e.accuracy for e in entries if e.accuracy > 0]
        if len(daily_rates) > 1:
            mean_r   = sum(daily_rates)/len(daily_rates)
            variance = sum((r-mean_r)**2 for r in daily_rates)/len(daily_rates)
            consistency = round(max(0.0, 100.0-(variance**0.5)*2), 1)
        else:
            consistency = 100.0 if daily_rates else 0.0

        if len(entries) >= 4:
            mid   = len(entries)//2
            older = entries[mid:]
            newer = entries[:mid]
            if staff_type == "checker":
                def _s(e): return ((e.checked or 0)+(e.errors_found or 0))/FIXED_HRS
                fh = sum(_s(e) for e in older)/len(older)
                sh = sum(_s(e) for e in newer)/len(newer)
            else:
                fh = sum(e.accuracy for e in older)/len(older)
                sh = sum(e.accuracy for e in newer)/len(newer)
            trend = "improving" if sh>fh+2 else "declining" if sh<fh-2 else "stable"
        else:
            trend = "stable"

        if staff_type == "purchaser":
            # Purchaser params using DB fields:
            # sales_bills_open = Total PO Bills Received
            # checked          = Purchase Bills Checked
            # picked           = Purchase Bill Entry
            # errors_found     = Number of Items
            # cs_sales_open    = CS in Purchase Open
            # packing_done     = CS in Purchase Received
            # rack_organized   = Items Racked
            pur_bills_received = tsb_normal           # Total PO Bills Received
            pur_bills_checked  = tck_normal            # Purchase Bills Checked
            pur_bill_entry     = tp                    # Purchase Bill Entry
            pur_items          = tck_urgent            # Number of Items (errors_found)
            pur_cs_open        = tcs                   # CS in Purchase Open
            pur_cs_received    = tpk                   # CS in Purchase Received
            pur_items_racked   = tro                   # Items Racked

            pur_bill_rate      = round(safe_div(pur_bills_checked, pur_bills_received) * 100, 1) if pur_bills_received > 0 else 0.0
            pur_pending_bills  = max(pur_bills_received - pur_bills_checked, 0)  # auto-calculated
            pur_cs_fulfilment  = round(min(safe_div(pur_cs_received, pur_cs_open) * 100, 100.0), 1) if pur_cs_open > 0 else 100.0
            pur_racking_eff    = round(safe_div(pur_items_racked, pur_items) * 100, 1) if pur_items > 0 else 0.0
            pur_speed          = round(safe_div(pur_items, ts_total), 1)

            # ── PURCHASER POINT SYSTEM — HARD DIFFICULTY (100 pts) ──
            pur_entry_rate  = round(min(safe_div(pur_bill_entry, max(pur_bills_received,1)), 1.0) * 100, 1)
            pur_pending_pct = max(0.0, 1.0 - safe_div(pur_pending_bills, max(pur_bills_received,1)))
            pur_sweep_score = safe_div(tsd, days) * 2
            eff_score = round(
                (pur_bill_rate     / 100) * 28 +
                (pur_cs_fulfilment / 100) * 23 +
                (pur_racking_eff   / 100) * 18 +
                min(pur_speed      / 60,  1.0) * 12 +
                (pur_entry_rate    / 100) * 8 +
                pur_pending_pct              * 5 +
                pur_sweep_score              * 2 + # sweep bonus
                (workspace_score   / 100) * 4,    # rack+table
                1)

        elif staff_type == "picker":
            # ── PICKER POINT SYSTEM — HARD DIFFICULTY (100 pts) ──
            # workspace_score already includes rack(5) + table(3) + sweep(2)
            eff_score = round(
                (pick_acc        / 100) * 25 +
                (bill_fulfilment / 100) * 20 +
                min(pick_speed   / 250, 1.0) * 18 +
                min(packing_eff  / 100, 1.0) * 12 +
                (workspace_score / 100) * 10 +   # rack+table+sweep all here
                min(cs_fulfilment/ 100, 1.0) * 8 +
                (consistency     / 100) * 7,
                1)
        else:
            # ── CHECKER POINT SYSTEM — HARD DIFFICULTY (100 pts) ──
            speed_score     = min(safe_div(check_speed, 80.0), 1.0) * 30
            clearance_score = (clearance_rate / 100.0) * 25
            accuracy_score  = (normal_pct / 100.0) * 20
            consist_score   = (consistency / 100.0) * 10
            poteff_score    = (potential_eff / 100.0) * 8
            volume_score    = min(safe_div(tck_total, 500.0), 1.0) * 5
            sweep_score     = safe_div(tsd, days) * 2  # sweep done yes/no
            eff_score       = round(speed_score + clearance_score + accuracy_score +
                                    consist_score + poteff_score + volume_score + sweep_score, 1)
        # Apply complaint deductions (minus marking) — now per-employee when target_emp_id is set
        complaint_deduction = get_complaint_deduction(staff_type, emp_id=emp_id)
        eff_score = max(round(eff_score - complaint_deduction, 1), 0.0)
        eff_score = min(eff_score, 100.0)

        if staff_type == "picker":
            # Point-based grade (medium-upper difficulty)
            if   eff_score >= 88: grade="ELITE"
            elif eff_score >= 72: grade="PROFICIENT"
            elif eff_score >= 52: grade="SATISFACTORY"
            else:                 grade="RE-TRAINING"
        elif staff_type == "purchaser":
            # Point-based grade (medium-upper difficulty)
            if   eff_score >= 88: grade="ELITE"
            elif eff_score >= 72: grade="PROFICIENT"
            elif eff_score >= 52: grade="SATISFACTORY"
            else:                 grade="RE-TRAINING"
        else:
            # Point-based grade (medium-upper difficulty)
            if   eff_score >= 88: grade="ELITE"
            elif eff_score >= 72: grade="PROFICIENT"
            elif eff_score >= 52: grade="SATISFACTORY"
            else:                 grade="RE-TRAINING"

        if staff_type == "checker":
            feedback_map = {
                "ELITE":        "Exceptional! High speed, clear bills, low error rate.",
                "PROFICIENT":   "Good performance. Push speed higher and reduce urgent items.",
                "SATISFACTORY": "Meets expectations. Focus on speed and error reduction.",
                "RE-TRAINING":  "Performance needs attention. Please speak to your manager.",
            }
        elif staff_type == "purchaser":
            feedback_map = {
                "ELITE":        "Outstanding procurement! All bills processed and CS fulfilled.",
                "PROFICIENT":   "Good procurement performance. Push CS fulfilment higher.",
                "SATISFACTORY": "Meets expectations. Focus on bill processing rate.",
                "RE-TRAINING":  "Performance needs attention. Please speak to your manager.",
            }
        else:
            feedback_map = {
                "ELITE":        "Outstanding performance. Keep it up!",
                "PROFICIENT":   "Strong results. Minor improvements will push you to Elite.",
                "SATISFACTORY": "Meets expectations. Keep pushing to reach Proficient.",
                "RE-TRAINING":  "Performance needs attention. Please speak to your manager.",
            }

        return dict(
            pick_acc=pick_acc, pick_speed=pick_speed,
            packing_eff=packing_eff, cs_fulfilment=cs_fulfilment,
            workspace_score=workspace_score,
            tp=tp, tm=tm, ti=ti, tcs=tcs, tro=tro, ttc=ttc,
            check_acc=check_acc, error_rate=error_rate,
            check_speed=check_speed, ck_speed=check_speed,
            normal_pct=normal_pct, urgent_pct=urgent_pct,
            tck=tck_total, tck_normal=tck_normal, tck_urgent=tck_urgent,
            tck_total=tck_total, ter=tck_urgent,
            tsb=tsb_normal, tsb_normal=tsb_normal,
            tsb_urgent=tsb_urgent, tsb_total=tsb_total,
            tpk=tpk, tpd=tpk,
            tbr=tbr, pending_bills=pending_bills, clearance_rate=clearance_rate,
            tbr_picker=tbr_picker, bill_fulfilment=bill_fulfilment,
            pur_bills_received=pur_bills_received if staff_type=="purchaser" else 0,
            pur_pending_bills=pur_pending_bills if staff_type=="purchaser" else 0,
            pur_entry_rate=pur_entry_rate if staff_type=="purchaser" else 0,
            pur_bills_checked=pur_bills_checked if staff_type=="purchaser" else 0,
            pur_bill_entry=pur_bill_entry if staff_type=="purchaser" else 0,
            pur_items=pur_items if staff_type=="purchaser" else 0,
            pur_cs_open=pur_cs_open if staff_type=="purchaser" else 0,
            pur_cs_received=pur_cs_received if staff_type=="purchaser" else 0,
            pur_items_racked=pur_items_racked if staff_type=="purchaser" else 0,
            pur_bill_rate=pur_bill_rate if staff_type=="purchaser" else 0,
            pur_cs_fulfilment=pur_cs_fulfilment if staff_type=="purchaser" else 0,
            pur_racking_eff=pur_racking_eff if staff_type=="purchaser" else 0,
            pur_speed=pur_speed if staff_type=="purchaser" else 0,
            complaint_deduction=complaint_deduction,
            tsd=tsd,
            eff_score=eff_score, grade=grade, days=days,
            consistency=consistency, trend=trend,
            feedback=feedback_map.get(grade,""),
            potential_items=potential_items, gap_items=gap_items,
            potential_eff=potential_eff,
            ts=round(ts_total,2), ttt=round(ts_total,2),
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
        if not session.get("user_id"): return redirect(url_for("login"))
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


def run_migrations():
    """Add missing columns to existing tables without dropping them."""
    try:
        is_postgres = "postgresql" in app.config["SQLALCHEMY_DATABASE_URI"]
        if is_postgres:
            # Migrate employees table
            emp_cols = [
                ("sunday_override", "BOOLEAN DEFAULT FALSE"),
                ("twofa_secret",    "VARCHAR(32)"),
                ("twofa_enabled",   "BOOLEAN DEFAULT FALSE"),
                ("created_at",      "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ]
            for col, col_type in emp_cols:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE employees ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                    db.session.commit()
                    logger.info(f"✅ Column '{col}' ensured on employees table")
                except Exception as ce:
                    db.session.rollback()
                    logger.warning(f"Column '{col}' migration skipped: {ce}")
            # Migrate kpi_entries table
            # Create complaints table if not exists
            try:
                db.session.execute(db.text("""
                    CREATE TABLE IF NOT EXISTS complaints (
                        id SERIAL PRIMARY KEY,
                        entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
                        reported_by INTEGER REFERENCES employees(id),
                        description TEXT NOT NULL,
                        complaint_type VARCHAR(50) DEFAULT 'delivery',
                        picker_deduct FLOAT DEFAULT 0,
                        checker_deduct FLOAT DEFAULT 0,
                        purchaser_deduct FLOAT DEFAULT 0,
                        picker_final FLOAT DEFAULT 0,
                        checker_final FLOAT DEFAULT 0,
                        purchaser_final FLOAT DEFAULT 0,
                        is_resolved BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                db.session.commit()
                logger.info("✅ Complaints table ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"Complaints table: {ce}")
            kpi_cols = [
                ("bills_received",      "INTEGER DEFAULT 0"),
                ("sweep_done",          "INTEGER DEFAULT 0"),
                ("pending_bills_manual","INTEGER DEFAULT 0"),
                ("total_bills_received","INTEGER DEFAULT 0"),
            ]
            for col, col_type in kpi_cols:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                    db.session.commit()
                    logger.info(f"✅ Column '{col}' ensured on kpi_entries table")
                except Exception as ce:
                    db.session.rollback()
                    logger.warning(f"kpi_entries column '{col}' migration skipped: {ce}")
            # Add target_emp_id to complaints (targeted minus-marking)
            try:
                db.session.execute(db.text(
                    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS target_emp_id INTEGER REFERENCES employees(id)"
                ))
                db.session.commit()
                logger.info("✅ complaints.target_emp_id ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"complaints.target_emp_id migration skipped: {ce}")
        else:
            # SQLite doesn't support IF NOT EXISTS on ALTER TABLE
            import sqlite3
            db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(employees)")
            existing = [row[1] for row in cursor.fetchall()]
            sqlite_cols = {
                "sunday_override": "BOOLEAN DEFAULT 0",
                "twofa_secret": "VARCHAR(32)",
                "twofa_enabled": "BOOLEAN DEFAULT 0",
            }
            for col, col_type in sqlite_cols.items():
                if col not in existing:
                    cursor.execute(f"ALTER TABLE employees ADD COLUMN {col} {col_type}")
                    logger.info(f"✅ SQLite column '{col}' added")
            # complaints.target_emp_id
            try:
                cursor.execute("PRAGMA table_info(complaints)")
                cmp_cols = [row[1] for row in cursor.fetchall()]
                if cmp_cols and "target_emp_id" not in cmp_cols:
                    cursor.execute("ALTER TABLE complaints ADD COLUMN target_emp_id INTEGER")
                    logger.info("✅ SQLite complaints.target_emp_id added")
            except Exception as sce:
                logger.warning(f"SQLite complaints migration: {sce}")
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Migration error: {e}")


def init_db():
    with app.app_context():
        try:
            db.create_all()
            # Run migrations for existing DBs
            run_migrations()
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
                logger.info("✅ Database initialized successfully")
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


@app.route("/favicon.ico")
def favicon():
    """Return empty favicon to stop session-killing redirect loop."""
    return Response(b"", status=204, mimetype="image/x-icon")


@app.route("/")
def index():
    if not session.get("user_id"): return redirect(url_for("login"))
    return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))
    if request.method == "POST":
        try:
            email = request.form.get("email", "").lower().strip()
            password = request.form.get("password", "")
            user = Employee.query.filter_by(email=email).first()

            if not user or not user.check_password(password):
                flash("Invalid Pharma ID or Password.", "danger")
                return render_template("login.html")

            # NOTE: staff_type is NEVER read from login form.
            # Role is set by admin only. Prevents staff gaming the leaderboard.

            session.clear()
            session.permanent = True
            session["user_id"] = user.id
            session["user_name"] = user.name
            session["staff_type"] = user.staff_type
            session["is_admin"] = bool(user.is_admin)

            logger.info(f"User login successful: {email}")
            return redirect(url_for("admin_dashboard") if user.is_admin else url_for("dashboard"))
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

                    sales_bills_open = gi("sales_bills_open")
                    cs_sales_open    = gi("cs_sales_open")
                    packing_done     = gi("packing_done")
                    total_mins       = min(gf("total_mins"), 480)
                    check_mins       = min(gf("check_mins"), 480)

                    # Defaults — always defined so later code never NameErrors
                    picked               = 0
                    missed               = 0
                    checked              = 0
                    errors_found         = 0
                    bills_received       = 0
                    pending_bills_manual = 0
                    total_bills_received = 0

                    if staff_type == "purchaser":
                        sales_bills_open = gi("sales_bills_open")   # PO Bills Received
                        checked          = gi("checked")             # PO Bills Checked
                        picked           = gi("picked")              # PO Bill Entry
                        errors_found     = gi("errors_found")        # Number of Items
                        cs_sales_open    = gi("cs_sales_open")       # CS in PO Open
                        packing_done     = gi("packing_done")        # CS in PO Received
                    elif staff_type == "checker":
                        checked              = gi("checked")
                        errors_found         = gi("errors_found")
                        picked               = gi("picked")
                        bills_received       = gi("bills_received")
                        pending_bills_manual = gi("pending_bills_manual")
                    else:  # picker
                        picked               = gi("picked")
                        missed               = gi("missed")
                        total_bills_received = gi("total_bills_received")

                    ne = KPIEntry(
                        emp_id               = emp_id,
                        sales_bills_open     = sales_bills_open,
                        picked               = picked,
                        missed               = missed,
                        cs_sales_open        = cs_sales_open,
                        packing_done         = packing_done,
                        rack_organized       = gi("rack_organized") if staff_type in ("picker","purchaser") else 0,
                        table_clean          = gi("table_clean"),
                        sweep_done           = gi("sweep_done"),
                        total_time           = 9.0,
                        checked              = checked,
                        errors_found         = errors_found,
                        check_time           = 9.0,
                        bills_received       = bills_received       if staff_type == "checker" else 0,
                        pending_bills_manual = pending_bills_manual if staff_type == "checker" else 0,
                        total_bills_received = total_bills_received if staff_type == "picker"  else 0,
                        entry_date=today
                    )
                    db.session.add(ne)
                    db.session.commit()

                    all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
                    all_stats = build_analytics(all_entries, staff_type, emp_id=emp_id)
                    if all_stats:
                        prev_best = session.get(f"pb_{emp_id}", 0)
                        if all_stats['eff_score'] > prev_best:
                            session[f"pb_{emp_id}"] = all_stats['eff_score']
                            new_personal_best = True

                    flash("✅ Metrics recorded successfully.", "success")
                    today_entry = ne

                    socketio.emit('entry_update', {'user': session.get('user_name'), 'accuracy': ne.accuracy})
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Dashboard POST: {e}")
                    flash("Error saving metrics.", "danger")

        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        d_stats = build_analytics(get_period_entries(emp_id, "day"), staff_type, emp_id=emp_id)
        w_stats = build_analytics(get_period_entries(emp_id, "week"), staff_type, emp_id=emp_id)
        m_stats = build_analytics(get_period_entries(emp_id, "month"), staff_type, emp_id=emp_id)

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

        # Find open windows where THIS staff hasn't submitted yet
        open_wins = PastEntryWindow.query.filter_by(is_active=True).all()
        pending_windows = []
        for w in open_wins:
            has_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=w.past_date).first()
            if not has_entry:
                pending_windows.append(w)

        return render_template("dashboard.html",
            user_name=session.get("user_name", "User"),
            user_id=emp_id,
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
            new_personal_best=new_personal_best,
            pending_windows=pending_windows
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return render_template("dashboard.html",
            user_name="User", user_id=0, staff_type="picker", today=date.today(),
            today_entry=None, d_stats=None, w_stats=None, m_stats=None,
            recent=[], trend_labels=[], trend_accuracy=[], trend_speed=[], total_entries=0,
            new_personal_best=False, pending_windows=[])


@app.route("/admin_dashboard")
@admin_required
def admin_dashboard():
    try:
        employees = Employee.query.filter_by(is_admin=False).options(db.joinedload(Employee.entries)).all()
        rows = []
        today = date.today()
        for emp in employees:
            ents = emp.entries
            stats = build_analytics(ents, emp.staff_type, emp_id=emp.id)
            week_ents = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
            week_stats = build_analytics(week_ents, emp.staff_type, emp_id=emp.id)
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
        open_windows = PastEntryWindow.query.filter_by(is_active=True).order_by(PastEntryWindow.past_date.desc()).all()
        open_complaints = Complaint.query.filter_by(is_resolved=False).order_by(Complaint.created_at.desc()).all()

        rws = [r for r in rows if r['stats']]
        def srt(lst, wk=False):
            k = 'week_stats' if wk else 'stats'
            return sorted([r for r in lst if r[k]], key=lambda r: r[k]['eff_score'], reverse=True)[:5]
        picker_lb       = srt([r for r in rws if r['emp'].staff_type=='picker'])
        checker_lb      = srt([r for r in rws if r['emp'].staff_type=='checker'])
        purchaser_lb    = srt([r for r in rws if r['emp'].staff_type=='purchaser'])
        mixed_lb        = srt(rws)
        rw2             = [r for r in rows if r['week_stats']]
        picker_week_lb  = srt([r for r in rw2 if r['emp'].staff_type=='picker'], wk=True)
        checker_week_lb = srt([r for r in rw2 if r['emp'].staff_type=='checker'], wk=True)
        purchaser_week_lb = srt([r for r in rw2 if r['emp'].staff_type=='purchaser'], wk=True)
        mixed_week_lb   = srt(rw2, wk=True)
        all_s = [r['stats'] for r in rws]
        team_avg_eff = round(sum(s['eff_score'] for s in all_s)/len(all_s),1) if all_s else 0
        team_avg_acc = round(sum(s.get('check_acc',0) if r['emp'].staff_type=='checker' else s.get('pick_acc',0) for r,s in [(r,r['stats']) for r in rws])/len(all_s),1) if all_s else 0
        grade_counts = {"ELITE":0,"PROFICIENT":0,"SATISFACTORY":0,"RE-TRAINING":0}
        for s in all_s:
            g = s.get("grade","RE-TRAINING")
            if g in grade_counts: grade_counts[g] += 1
        needs_attention = [r for r in rws if r["stats"].get("grade")=="RE-TRAINING"]
        improving = [r for r in rw2 if r["week_stats"].get("trend")=="improving"]

        return render_template("admin.html",
            rows=rows, total_picked=total_picked, total_entries=total_entries,
            active_today=active_today, emp_count=len(employees), today=today,
            open_windows=open_windows,
            open_complaints=open_complaints,
            all_staff=employees,
            picker_lb=picker_lb, checker_lb=checker_lb, purchaser_lb=purchaser_lb, mixed_lb=mixed_lb,
            picker_week_lb=picker_week_lb, checker_week_lb=checker_week_lb, purchaser_week_lb=purchaser_week_lb, mixed_week_lb=mixed_week_lb,
            team_avg_eff=team_avg_eff, team_avg_acc=team_avg_acc,
            grade_counts=grade_counts, needs_attention=needs_attention, improving=improving,
        )
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        flash("Error loading admin dashboard.", "danger")
        return render_template("admin.html", rows=[], total_picked=0,
                               total_entries=0, active_today=0, emp_count=0, today=date.today(),
                               open_windows=[], open_complaints=[], picker_lb=[], checker_lb=[], purchaser_lb=[], mixed_lb=[],
                               picker_week_lb=[], checker_week_lb=[], purchaser_week_lb=[], mixed_week_lb=[],
                               team_avg_eff=0, team_avg_acc=0, improving=[],
                               grade_counts={"ELITE":0,"PROFICIENT":0,"SATISFACTORY":0,"RE-TRAINING":0},
                               needs_attention=[])


@app.route("/staff/<int:emp_id>")
@login_required
def staff_detail(emp_id):
    # Admins can view any; staff can only view themselves
    if not session.get("is_admin") and session.get("user_id") != emp_id:
        return redirect(url_for("dashboard"))
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))

        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        today = date.today()

        a_stats = build_analytics(entries, emp.staff_type, emp_id=emp.id)
        d_stats = build_analytics(get_period_entries(emp_id, "day"), emp.staff_type, emp_id=emp.id)
        w_stats = build_analytics(get_period_entries(emp_id, "week"), emp.staff_type, emp_id=emp.id)
        m_stats = build_analytics(get_period_entries(emp_id, "month"), emp.staff_type, emp_id=emp.id)

        # Heatmap: last 30 days — use accuracy for pickers, check_rate for checkers
        heatmap = {}
        for e in entries:
            delta = (today - e.entry_date).days
            if delta <= 29:
                if emp.staff_type == "checker":
                    tck_day = (e.checked or 0) + (e.errors_found or 0)
                    daily_spd = round(tck_day / 9.0, 1)
                    heatmap[str(e.entry_date)] = min(round(daily_spd / 50 * 100, 1), 100)
                else:
                    heatmap[str(e.entry_date)] = e.accuracy

        # Compute ranking and build leaderboards (role + overall)
        role_leaderboard = []
        overall_leaderboard = []
        emp_overall_rank = None
        emp_role_rank = None
        try:
            all_staff   = Employee.query.filter_by(is_admin=False).all()
            all_sc, role_sc = [], []
            _stats_by_id = {}
            _name_by_id  = {s.id: (s.name, s.staff_type) for s in all_staff}
            for s in all_staff:
                s_stats = build_analytics(KPIEntry.query.filter_by(emp_id=s.id).all(), s.staff_type, emp_id=s.id)
                if s_stats:
                    _stats_by_id[s.id] = s_stats
                    all_sc.append((s.id, s_stats['eff_score']))
                    if s.staff_type == emp.staff_type:
                        role_sc.append((s.id, s_stats['eff_score']))
            all_sc.sort(key=lambda x: x[1], reverse=True)
            role_sc.sort(key=lambda x: x[1], reverse=True)
            emp_overall_rank = next((i+1 for i,(sid,_) in enumerate(all_sc)  if sid==emp.id), None)
            emp_role_rank    = next((i+1 for i,(sid,_) in enumerate(role_sc) if sid==emp.id), None)

            # Top 5 leaderboards (and always include this staff member's position)
            def _build_lb(sorted_list, limit=5):
                out = []
                for rank, (sid, score) in enumerate(sorted_list[:limit], start=1):
                    nm, st = _name_by_id.get(sid, ("—", ""))
                    st_stats = _stats_by_id.get(sid, {})
                    out.append({
                        "rank": rank,
                        "id": sid,
                        "name": nm,
                        "staff_type": st,
                        "score": score,
                        "grade": st_stats.get("grade", "—"),
                        "is_self": sid == emp.id,
                    })
                # Ensure this employee shown even if outside top 5
                if not any(r["is_self"] for r in out):
                    for rank, (sid, score) in enumerate(sorted_list, start=1):
                        if sid == emp.id:
                            nm, st = _name_by_id.get(sid, ("—", ""))
                            st_stats = _stats_by_id.get(sid, {})
                            out.append({
                                "rank": rank,
                                "id": sid,
                                "name": nm,
                                "staff_type": st,
                                "score": score,
                                "grade": st_stats.get("grade", "—"),
                                "is_self": True,
                            })
                            break
                return out

            role_leaderboard    = _build_lb(role_sc)
            overall_leaderboard = _build_lb(all_sc)
        except Exception as re_err:
            logger.error(f"staff_detail ranking: {re_err}")

        return render_template("staff_detail.html",
            emp=emp,
            emp_overall_rank=emp_overall_rank,
            emp_role_rank=emp_role_rank,
            role_leaderboard=role_leaderboard,
            overall_leaderboard=overall_leaderboard,
            entries=entries,
            a_stats=a_stats,
            d_stats=d_stats,
            w_stats=w_stats,
            m_stats=m_stats,
            heatmap=heatmap,
            today=today,
            is_admin=bool(session.get("is_admin"))
        )
    except Exception as e:
        logger.error(f"Staff detail error: {e}")
        flash("Error loading staff profile.", "danger")
        return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))


@app.route("/admin/toggle_sunday/<int:emp_id>", methods=["POST"])
@admin_required
def admin_toggle_sunday(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if emp and not emp.is_admin:
            emp.sunday_override = not emp.sunday_override
            db.session.commit()
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

        emp = Employee(name=name, email=email, staff_type=staff_type, role=f"Operations {staff_type.title() if staff_type != 'purchaser' else 'Purchaser'}")
        emp.set_password(password)
        db.session.add(emp)
        db.session.commit()
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
        if staff_type in ("picker", "checker", "purchaser"):
            emp.staff_type = staff_type
            emp.role = f"Operations {staff_type.title()}"
        if new_password:
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
                return redirect(url_for("admin_dashboard"))
            emp.set_password(new_password)

        db.session.commit()
        log_audit("edit_user", name, "Updated by admin")
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
        # Guard against CSV injection — strip leading =, +, -, @, tab, CR from cells
        def _csv_safe(v):
            if v is None:
                return ""
            s = str(v)
            if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
                return "'" + s
            return s

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
                    _csv_safe(emp.name), _csv_safe(emp.email), _csv_safe(emp.staff_type),
                    e.entry_date,
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


# Alias for staff_detail template compatibility
@app.route("/export_data")
@admin_required
def export_data():
    return redirect(url_for("admin_export_csv"))


@app.route("/export_pdf")
@login_required
def export_pdf():
    try:
        from utils import generate_visual_pdf
        emp_id = session.get("user_id")
        emp = db.session.get(Employee, emp_id)
        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()

        payload = {
            "all_stats": build_analytics(all_entries, emp.staff_type, emp_id=emp.id),
            "day_stats": build_analytics(get_period_entries(emp_id, "day"), emp.staff_type, emp_id=emp.id),
            "week_stats": build_analytics(get_period_entries(emp_id, "week"), emp.staff_type, emp_id=emp.id),
            "month_stats": build_analytics(get_period_entries(emp_id, "month"), emp.staff_type, emp_id=emp.id),
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


@app.route("/download/<int:emp_id>")
@login_required
def download_pdf(emp_id):
    if not session.get("is_admin") and session.get("user_id") != emp_id:
        return redirect(url_for("dashboard"))
    try:
        from utils import generate_visual_pdf
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()

        payload = {
            "all_stats": build_analytics(all_entries, emp.staff_type, emp_id=emp.id),
            "day_stats": build_analytics(get_period_entries(emp_id, "day"), emp.staff_type, emp_id=emp.id),
            "week_stats": build_analytics(get_period_entries(emp_id, "week"), emp.staff_type, emp_id=emp.id),
            "month_stats": build_analytics(get_period_entries(emp_id, "month"), emp.staff_type, emp_id=emp.id),
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
        logger.error(f"Download PDF error: {e}")
        flash("Error generating PDF.", "danger")
        return redirect(url_for("admin_dashboard"))


# ─── PAST DATE ENTRY WINDOW ──────────────────────────────────────────────────

@app.route("/admin/past_window", methods=["POST"])
@admin_required
def admin_open_past_window():
    """Admin opens (or closes) a past-date entry window."""
    try:
        action    = request.form.get("action", "open")
        date_str  = request.form.get("past_date", "").strip()
        if not date_str:
            flash("Please select a date.", "warning")
            return redirect(url_for("admin_dashboard"))

        past_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today     = date.today()

        if past_date >= today:
            flash("You can only open windows for past dates (before today).", "warning")
            return redirect(url_for("admin_dashboard"))

        window = PastEntryWindow.query.filter_by(past_date=past_date).first()

        if action == "close":
            if window:
                window.is_active = False
                db.session.commit()
                log_audit("close_past_window", str(past_date), "Admin closed past entry window")
                flash(f"✅ Entry window for {past_date.strftime('%d %b %Y')} closed.", "success")
            else:
                flash("No window found for that date.", "warning")
        else:
            if window:
                window.is_active  = True
                window.opened_by  = session.get("user_id")
                window.opened_at  = datetime.utcnow()
            else:
                window = PastEntryWindow(
                    past_date  = past_date,
                    opened_by  = session.get("user_id"),
                    is_active  = True
                )
                db.session.add(window)
            db.session.commit()
            log_audit("open_past_window", str(past_date), "Admin opened past entry window")
            flash(f"✅ Past entry window opened for {past_date.strftime('%d %b %Y')}. "
                  f"Staff who haven't submitted can now enter their data.", "success")

    except ValueError:
        flash("Invalid date format.", "danger")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_open_past_window: {e}")
        flash("Error updating entry window.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/past_entry/<date_str>", methods=["GET", "POST"])
@login_required
def past_entry(date_str):
    """Staff submits KPI data for an admin-opened past date."""
    try:
        past_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid date.", "danger")
        return redirect(url_for("dashboard"))

    emp_id     = session.get("user_id")
    staff_type = session.get("staff_type", "picker")
    today      = date.today()

    if past_date >= today:
        flash("You can only submit data for past dates.", "warning")
        return redirect(url_for("dashboard"))

    window = PastEntryWindow.query.filter_by(past_date=past_date, is_active=True).first()
    if not window:
        flash("No active entry window for that date.", "warning")
        return redirect(url_for("dashboard"))

    existing = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=past_date).first()
    if existing:
        flash(f"You have already submitted data for {past_date.strftime('%d %b %Y')}.", "info")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        try:
            def gi(k): return max(0, int(request.form.get(k, 0) or 0))

            # 9hr constant — no time input field
            FIXED_TIME = 9.0

            # Role-specific field extraction
            if staff_type == "purchaser":
                sales_bills_open     = gi("sales_bills_open")
                checked              = gi("checked")
                picked               = gi("picked")
                errors_found         = gi("errors_found")
                cs_sales_open        = gi("cs_sales_open")
                packing_done         = gi("packing_done")
                rack_organized       = gi("rack_organized")
                table_clean          = 0
                missed               = 0
                bills_received       = 0
                pending_bills_manual = 0
                total_bills_received = 0
            elif staff_type == "checker":
                sales_bills_open     = gi("sales_bills_open")
                checked              = gi("checked")
                errors_found         = gi("errors_found")
                picked               = gi("picked")
                missed               = 0
                cs_sales_open        = gi("cs_sales_open")
                packing_done         = gi("packing_done")
                rack_organized       = 0
                table_clean          = 0
                bills_received       = gi("bills_received")
                pending_bills_manual = gi("pending_bills_manual")
                total_bills_received = 0
            else:  # picker
                sales_bills_open     = gi("sales_bills_open")
                picked               = gi("picked")
                missed               = gi("missed")
                cs_sales_open        = gi("cs_sales_open")
                packing_done         = gi("packing_done")
                rack_organized       = gi("rack_organized")
                table_clean          = gi("table_clean")
                checked              = 0
                errors_found         = 0
                bills_received       = 0
                pending_bills_manual = 0
                total_bills_received = gi("total_bills_received")

            ne = KPIEntry(
                emp_id               = emp_id,
                sales_bills_open     = sales_bills_open,
                picked               = picked,
                missed               = missed,
                cs_sales_open        = cs_sales_open,
                packing_done         = packing_done,
                rack_organized       = rack_organized,
                table_clean          = table_clean,
                total_time           = FIXED_TIME,
                checked              = checked,
                errors_found         = errors_found,
                check_time           = FIXED_TIME,
                bills_received       = bills_received,
                pending_bills_manual = pending_bills_manual,
                total_bills_received = total_bills_received,
                sweep_done           = gi("sweep_done"),
                entry_date           = past_date,
            )
            db.session.add(ne)
            db.session.commit()
            log_audit("past_entry_submit", session.get("user_name", ""), f"Submitted past data for {past_date}")
            flash(f"✅ Past entry for {past_date.strftime('%d %b %Y')} saved successfully.", "success")
            return redirect(url_for("dashboard"))
        except Exception as e:
            db.session.rollback()
            logger.error(f"past_entry POST: {e}")
            flash("Error saving past entry.", "danger")

    return render_template("past_entry.html",
        past_date  = past_date,
        staff_type = staff_type,
        user_name  = session.get("user_name", "")
    )


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
        stats = build_analytics(entries, emp.staff_type, emp_id=emp.id)
        return jsonify(stats=stats, name=emp.name, staff_type=emp.staff_type)
    except Exception as e:
        logger.error(f"api_stats: {e}")
        return jsonify(error="Server error"), 500


@socketio.on('connect')
def handle_connect():
    emit('connected', {'message': 'Connected to KPI tracker'})


# ─── ERROR HANDLERS ──────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return redirect(url_for("login"))

@app.errorhandler(500)
def server_error(e):
    logger.error(f"500: {e}")
    return redirect(url_for("login"))


# ─── MAIN ────────────────────────────────────────────────────────────────────


@app.route("/admin/complaint", methods=["POST"])
@admin_required
def admin_add_complaint():
    """Admin enters a post-delivery complaint with suggested deductions."""
    try:
        description    = request.form.get("description", "").strip()
        complaint_type = request.form.get("complaint_type", "delivery")
        mistake_count  = max(1, int(request.form.get("mistake_count", 1) or 1))
        target_emp_id  = request.form.get("target_emp_id", "").strip()

        # Validate target_emp_id (optional; empty = applies to whole role)
        target_id = None
        if target_emp_id:
            try:
                target_id = int(target_emp_id)
                if not db.session.get(Employee, target_id):
                    target_id = None
            except (ValueError, TypeError):
                target_id = None

        if not description:
            flash("Please describe the complaint.", "warning")
            return redirect(url_for("admin_dashboard"))

        # ── Role-based auto-suggestion system ──────────────────
        # Each mistake type has base penalty; multiplied by count; capped
        # Difficulty: HARD — meaningful deductions that affect rankings
        BASE = {"delivery": 4.0, "quality": 5.0, "missing": 6.0, "damage": 5.0}
        base = BASE.get(complaint_type, 4.0)
        multiplier = min(mistake_count, 8)  # cap at 8x

        # Responsibility weights per complaint type (must sum to ~2.0)
        WEIGHTS = {
            "delivery": {"picker": 1.0, "checker": 0.6, "purchaser": 0.2},  # picker packed wrong
            "quality":  {"picker": 0.2, "checker": 0.5, "purchaser": 1.0},  # purchaser sourced bad
            "missing":  {"picker": 1.0, "checker": 0.7, "purchaser": 0.1},  # picker missed item
            "damage":   {"picker": 0.5, "checker": 0.3, "purchaser": 0.8},  # purchaser/storage
        }
        w = WEIGHTS.get(complaint_type, WEIGHTS["delivery"])
        sug_picker    = min(round(base * multiplier * w["picker"],    1), 25.0)
        sug_checker   = min(round(base * multiplier * w["checker"],   1), 25.0)
        sug_purchaser = min(round(base * multiplier * w["purchaser"], 1), 25.0)

        # Use admin-entered finals if provided, else use suggestions (handle empty string)
        def _pf(key, default):
            v = request.form.get(key, "").strip()
            if not v:
                return default
            try:
                return float(v)
            except (ValueError, TypeError):
                return default
        picker_final    = _pf("picker_final",    sug_picker)
        checker_final   = _pf("checker_final",   sug_checker)
        purchaser_final = _pf("purchaser_final", sug_purchaser)

        complaint = Complaint(
            entry_date       = date.today(),
            reported_by      = session.get("user_id"),
            target_emp_id    = target_id,
            description      = description,
            complaint_type   = complaint_type,
            picker_deduct    = sug_picker,
            checker_deduct   = sug_checker,
            purchaser_deduct = sug_purchaser,
            picker_final     = picker_final,
            checker_final    = checker_final,
            purchaser_final  = purchaser_final,
        )
        db.session.add(complaint)
        db.session.commit()
        tgt_note = f" → {db.session.get(Employee, target_id).name}" if target_id else " (applied to role)"
        log_audit("complaint_added", description[:50], f"type={complaint_type} mistakes={mistake_count}{tgt_note}")
        flash(f"✅ Complaint recorded{tgt_note}. Deductions — Picker: {picker_final}pts, Checker: {checker_final}pts, Purchaser: {purchaser_final}pts", "warning")

    except Exception as e:
        db.session.rollback()
        logger.error(f"complaint: {e}")
        flash("Error recording complaint.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/complaint/<int:cid>/resolve", methods=["POST"])
@admin_required
def admin_resolve_complaint(cid):
    """Mark complaint as resolved."""
    try:
        c = db.session.get(Complaint, cid)
        if c:
            c.is_resolved = True
            db.session.commit()
            flash("✅ Complaint marked as resolved.", "success")
    except Exception as e:
        db.session.rollback()
    return redirect(url_for("admin_dashboard"))




@app.route("/admin/export_full")
@admin_required
def admin_export_full():
    """Export ALL data as JSON — for migration to Supabase."""
    try:
        import json
        employees = Employee.query.filter_by(is_admin=False).all()
        data = {"employees": [], "kpi_entries": [], "exported_at": str(date.today())}

        for emp in employees:
            data["employees"].append({
                "name": emp.name,
                "email": emp.email,
                "staff_type": emp.staff_type,
                "role": emp.role,
                # password_hash intentionally excluded — do not leak credentials in exports
                "sunday_override": emp.sunday_override,
            })
            entries = KPIEntry.query.filter_by(emp_id=emp.id).all()
            for e in entries:
                data["kpi_entries"].append({
                    "employee_email": emp.email,
                    "entry_date": str(e.entry_date),
                    "sales_bills_open": e.sales_bills_open or 0,
                    "picked": e.picked or 0,
                    "missed": e.missed or 0,
                    "cs_sales_open": e.cs_sales_open or 0,
                    "packing_done": e.packing_done or 0,
                    "rack_organized": e.rack_organized or 0,
                    "table_clean": e.table_clean or 0,
                    "checked": e.checked or 0,
                    "errors_found": e.errors_found or 0,
                    "bills_received": e.bills_received or 0,
                    "pending_bills_manual": e.pending_bills_manual or 0,
                    "total_bills_received": e.total_bills_received or 0,
                })

        json_str = json.dumps(data, indent=2)
        return Response(
            json_str,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=pharmaip_full_export_{date.today()}.json"}
        )
    except Exception as e:
        logger.error(f"export_full: {e}")
        flash("Error exporting data.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/admin/export_all_pdf")
@admin_required
def admin_export_all_pdf():
    """Generate PDF for every staff member and return as ZIP."""
    try:
        import zipfile
        from utils import generate_visual_pdf

        employees = Employee.query.filter_by(is_admin=False).all()
        if not employees:
            flash("No staff found.", "warning")
            return redirect(url_for("admin_dashboard"))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for emp in employees:
                try:
                    entries = KPIEntry.query.filter_by(emp_id=emp.id).all()
                    all_stats   = build_analytics(entries, emp.staff_type, emp_id=emp.id)
                    day_stats   = build_analytics(get_period_entries(emp.id, "day"), emp.staff_type, emp_id=emp.id)
                    week_stats  = build_analytics(get_period_entries(emp.id, "week"), emp.staff_type, emp_id=emp.id)
                    month_stats = build_analytics(get_period_entries(emp.id, "month"), emp.staff_type, emp_id=emp.id)

                    payload = {
                        "all_stats":   all_stats,
                        "day_stats":   day_stats,
                        "week_stats":  week_stats,
                        "month_stats": month_stats,
                        "staff_type":  emp.staff_type,
                        "all_entries": entries[-30:],
                    }
                    pdf_buf = generate_visual_pdf(emp.name, payload)
                    safe_name = emp.name.replace(" ", "_").replace("/", "-")
                    zf.writestr(f"KRA_{safe_name}_{date.today()}.pdf", pdf_buf.getvalue())
                except Exception as e:
                    logger.error(f"PDF for {emp.name}: {e}")
                    continue

        zip_buf.seek(0)
        log_audit("export_all_pdf", "all_staff", f"{len(employees)} PDFs generated")
        return Response(
            zip_buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=AllStaff_KRA_{date.today()}.zip"}
        )
    except Exception as e:
        logger.error(f"export_all_pdf: {e}")
        flash("Error generating PDFs.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/admin/migrate_checker/<int:emp_id>", methods=["POST"])
@admin_required
def migrate_checker_data(emp_id):
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp: return jsonify(error="Not found"), 404
        entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
        migrated = 0
        for e in entries:
            if (e.picked or 0) > 0 and (e.checked or 0) == 0:
                e.checked = e.picked; e.errors_found = e.missed; e.picked = 0; e.missed = 0
                migrated += 1
        emp.staff_type = "checker"
        db.session.commit()
        log_audit("migrate_checker", emp.name, f"Converted {migrated} entries picker->checker")
        return jsonify(success=True, employee=emp.name, entries_migrated=migrated)
    except Exception as ex:
        db.session.rollback()
        logger.error(f"migrate_checker: {ex}")
        return jsonify(error=str(ex)), 500


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
