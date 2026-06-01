import eventlet
eventlet.monkey_patch()

import os
import logging
import io
import csv
import random
import string
import requests as _requests
from datetime import date, timedelta, datetime
from collections import Counter
from functools import wraps
from typing import List, Optional, Dict, Any

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from flask_socketio import SocketIO, emit
from flask_mail import Mail, Message as MailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
IS_PRODUCTION = os.environ.get("RENDER") or os.environ.get("DATABASE_URL")

_secret_key = os.environ.get("SECRET_KEY", "pharma_secure_key_2024")
if IS_PRODUCTION and _secret_key == "pharma_secure_key_2024":
    logger.critical("⛔ SECRET_KEY is not set! Sessions are insecure in production. Set SECRET_KEY env var.")

app.config.update(
    SECRET_KEY=_secret_key,
    SESSION_COOKIE_SECURE=bool(IS_PRODUCTION),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=False,
    PREFERRED_URL_SCHEME='https',
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300},
    # Flask-Mail config
    MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", 587)),
    MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower() != "false",
    MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
    MAIL_DEFAULT_SENDER=os.environ.get("MAIL_USERNAME", "noreply@pharmaip.com"),
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
mail = Mail(app)
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
    admin_adjustment = db.Column(db.Float, default=0.0)  # Ongoing +/- pts applied to every score
    admin_adjustment_note = db.Column(db.String(200), default="")  # Reason/notes
    custom_hourly_target = db.Column(db.Float, nullable=True)  # Override default role target (Phase 1 auto-raise)
    phone         = db.Column(db.String(20), nullable=True)   # Feature 15: phone for OTP reset
    secondary_staff_type = db.Column(db.String(20), nullable=True)  # optional 2nd role for multitasking
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
    admin_adjustment = db.Column(db.Float, default=0.0)  # Admin Adjustment — manual +/- points override per entry

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

    shift_note = db.Column(db.String(300), nullable=True)  # Feature 1: Shift Note

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


class BillValidation(db.Model):
    """
    Constant Protocol — 4-way bill count validation.
    One row per (picker, date). Picker submits their count, then 3 checkers
    independently submit theirs. All 4 must match for the picker's KPI entry
    to be 'confirmed'. Mismatches are logged and flagged for admin.
    """
    __tablename__ = "bill_validations"
    id            = db.Column(db.Integer, primary_key=True)
    entry_date    = db.Column(db.Date, nullable=False, index=True)
    picker_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    picker_count  = db.Column(db.Integer, nullable=False)
    # Three independent checker submissions
    checker1_id    = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    checker1_count = db.Column(db.Integer, nullable=True)
    checker1_at    = db.Column(db.DateTime, nullable=True)
    checker2_id    = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    checker2_count = db.Column(db.Integer, nullable=True)
    checker2_at    = db.Column(db.DateTime, nullable=True)
    checker3_id    = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    checker3_count = db.Column(db.Integer, nullable=True)
    checker3_at    = db.Column(db.DateTime, nullable=True)
    # pending | confirmed | mismatch | admin_override
    status        = db.Column(db.String(20), default="pending", index=True)
    # Comma-separated list of emp ids that entered wrong values (for deductions)
    mismatch_emp_ids = db.Column(db.String(100), default="")
    # Admin override reason if applicable
    override_by   = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    override_note = db.Column(db.String(200), default="")
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("picker_id", "entry_date", name="_picker_date_uc"),
    )

    @property
    def checker_count_submitted(self):
        """How many of the 3 checker slots are filled."""
        return sum(1 for v in [self.checker1_count, self.checker2_count, self.checker3_count] if v is not None)

    @property
    def is_complete(self):
        return self.checker_count_submitted >= 3

    @property
    def all_match(self):
        if not self.is_complete: return False
        vals = [self.picker_count, self.checker1_count, self.checker2_count, self.checker3_count]
        return len(set(vals)) == 1

    def evaluate(self):
        """Re-evaluate status. Returns (new_status, mismatch_ids_list)."""
        if not self.is_complete:
            return "pending", []
        vals_with_ids = [
            (self.picker_id, self.picker_count),
            (self.checker1_id, self.checker1_count),
            (self.checker2_id, self.checker2_count),
            (self.checker3_id, self.checker3_count),
        ]
        # Find the mode (most common value) — those who disagree are flagged
        counts = Counter(v for _, v in vals_with_ids if v is not None)
        if len(counts) == 1:
            return "confirmed", []
        majority_val, _ = counts.most_common(1)[0]
        wrong = [emp_id for emp_id, v in vals_with_ids if v != majority_val and emp_id is not None]
        return "mismatch", wrong


# ─── NEW MODELS (Features 2-5) ───────────────────────────────────────────────

class Announcement(db.Model):
    """Feature 2: Notice Board."""
    __tablename__ = "announcements"
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(150), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("employees.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active  = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime, nullable=True)


class AnnouncementRead(db.Model):
    """Feature 2: Track which staff dismissed which announcement."""
    __tablename__ = "announcement_reads"
    id              = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id", ondelete="CASCADE"))
    emp_id          = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"))
    read_at         = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("announcement_id", "emp_id", name="_ann_emp_uc"),
    )


class StaffGoal(db.Model):
    """Feature 3: Personal monthly goal."""
    __tablename__ = "staff_goals"
    id           = db.Column(db.Integer, primary_key=True)
    emp_id       = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    target_score = db.Column(db.Float, nullable=False)
    month        = db.Column(db.String(7), nullable=False)  # e.g. "2025-05"
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "month", name="_goal_emp_month_uc"),
    )


class PastEntryRequest(db.Model):
    """Feature 4: Staff-requested past entry."""
    __tablename__ = "past_entry_requests"
    id             = db.Column(db.Integer, primary_key=True)
    emp_id         = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    requested_date = db.Column(db.Date, nullable=False)
    reason         = db.Column(db.String(200), nullable=False)
    status         = db.Column(db.String(20), default="pending")  # pending/approved/rejected
    admin_note     = db.Column(db.String(200), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at    = db.Column(db.DateTime, nullable=True)


class Badge(db.Model):
    """Feature 5: Badge definitions."""
    __tablename__ = "badges"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    icon        = db.Column(db.String(10), nullable=False)
    badge_type  = db.Column(db.String(20), default="auto")  # 'auto' or 'manual'


class StaffBadge(db.Model):
    """Feature 5: Badge awards to staff."""
    __tablename__ = "staff_badges"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    badge_id   = db.Column(db.Integer, db.ForeignKey("badges.id", ondelete="CASCADE"), nullable=False)
    awarded_at = db.Column(db.DateTime, default=datetime.utcnow)
    awarded_by = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    note       = db.Column(db.String(200), default="")
    __table_args__ = (
        db.UniqueConstraint("emp_id", "badge_id", name="_staff_badge_uc"),
    )


# ─── NEW MODELS (Features 14-17) ─────────────────────────────────────────────

class AdminStaffNote(db.Model):
    """Feature 14: Timestamped admin notes per staff member."""
    __tablename__ = "admin_staff_notes"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    note       = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PasswordResetOTP(db.Model):
    """Feature 15: Phone-based OTP for password reset."""
    __tablename__ = "password_reset_otps"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    otp_code   = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    is_used    = db.Column(db.Boolean, default=False)


class MonthlyReportArchive(db.Model):
    """Feature 16: Archive of auto-generated monthly PDF reports."""
    __tablename__ = "monthly_report_archives"
    id           = db.Column(db.Integer, primary_key=True)
    month_str    = db.Column(db.String(7), unique=True, nullable=False)  # e.g. "2025-04"
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    emp_count    = db.Column(db.Integer, default=0)
    file_path    = db.Column(db.String(200), nullable=True)


class MultitaskEntry(db.Model):
    """Secondary-role work logged by staff on the same day (multitasking).
    Captures the chosen Role-2's own KPI parameters so the admin sees a full
    report for both the default role (KPIEntry) and the multitask role."""
    __tablename__ = "multitask_entries"
    id             = db.Column(db.Integer, primary_key=True)
    emp_id         = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    entry_date     = db.Column(db.Date, nullable=False)
    secondary_type = db.Column(db.String(20), nullable=False)  # picker/checker/purchaser/delivery/billing
    # Role-specific KPI parameters (mirror KPIEntry fields; only relevant ones used per role)
    sales_bills_open     = db.Column(db.Integer, default=0)
    picked               = db.Column(db.Integer, default=0)
    missed               = db.Column(db.Integer, default=0)
    checked              = db.Column(db.Integer, default=0)
    errors_found         = db.Column(db.Integer, default=0)
    packing_done         = db.Column(db.Integer, default=0)
    cs_sales_open        = db.Column(db.Integer, default=0)
    total_bills_received = db.Column(db.Integer, default=0)
    bills_received       = db.Column(db.Integer, default=0)
    pending_bills_manual = db.Column(db.Integer, default=0)
    quantity       = db.Column(db.Integer, default=0)
    note           = db.Column(db.String(300), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "entry_date", "secondary_type", name="_mt_emp_date_role_uc"),
        db.Index("idx_mt_emp_date", "emp_id", "entry_date"),
    )

    @property
    def summary(self):
        """Short human-readable summary of the multitask role's parameters."""
        st = self.secondary_type
        if st == "picker":
            return f"{self.picked or 0} picked / {self.missed or 0} missed · {self.sales_bills_open or 0} bills"
        if st == "checker":
            return f"{self.checked or 0} checked / {self.errors_found or 0} urgent · {self.bills_received or 0} bills"
        if st == "purchaser":
            return f"{self.checked or 0} PO checked · {self.sales_bills_open or 0} PO bills · {self.errors_found or 0} items"
        # delivery / billing / packing / other
        return f"{self.quantity or 0} done"


class DeliveryBillerNote(db.Model):
    """Daily operational note from Delivery or Biller staff."""
    __tablename__ = "delivery_biller_notes"
    id           = db.Column(db.Integer, primary_key=True)
    emp_id       = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    entry_date   = db.Column(db.Date, nullable=False)
    note_type    = db.Column(db.String(20), nullable=False)  # 'delivery' or 'biller'
    note_text    = db.Column(db.Text, nullable=False)
    quantity     = db.Column(db.Integer, default=0)   # deliveries made / bills processed
    issues_count = db.Column(db.Integer, default=0)   # complaints / errors logged
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "entry_date", name="_dbnote_emp_date_uc"),
        db.Index("idx_dbn_date", "entry_date"),
    )


class DeliveryAssignment(db.Model):
    """Purchaser assigns a package to a delivery employee with destination info."""
    __tablename__ = "delivery_assignments"
    id               = db.Column(db.Integer, primary_key=True)
    purchaser_id     = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    delivery_emp_id  = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    destination_addr = db.Column(db.Text, nullable=False)
    destination_lat  = db.Column(db.Float, nullable=True)   # null if address-only
    destination_lng  = db.Column(db.Float, nullable=True)
    package_desc     = db.Column(db.Text, nullable=False)
    bills_count      = db.Column(db.Integer, default=0)
    recipient_name   = db.Column(db.String(150), nullable=True)
    company_name     = db.Column(db.String(150), nullable=True)
    # status: pending | in_transit | delivered | failed
    status           = db.Column(db.String(20), default="pending", index=True)
    assigned_at      = db.Column(db.DateTime, default=datetime.utcnow)
    notes            = db.Column(db.Text, nullable=True)
    __table_args__ = (
        db.Index("idx_da_delivery_emp", "delivery_emp_id"),
        db.Index("idx_da_status", "status"),
    )


class DeliveryTrip(db.Model):
    """GPS-tracked delivery trip for a DeliveryAssignment."""
    __tablename__ = "delivery_trips"
    id                   = db.Column(db.Integer, primary_key=True)
    assignment_id        = db.Column(db.Integer, db.ForeignKey("delivery_assignments.id", ondelete="CASCADE"), nullable=False, unique=True)
    emp_id               = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    trip_date            = db.Column(db.Date, nullable=False, default=date.today)
    departure_lat        = db.Column(db.Float, nullable=True)
    departure_lng        = db.Column(db.Float, nullable=True)
    departure_time       = db.Column(db.DateTime, nullable=True)
    arrival_time         = db.Column(db.DateTime, nullable=True)
    delivered_to_name    = db.Column(db.String(150), nullable=True)
    delivered_to_company = db.Column(db.String(150), nullable=True)
    delivery_note        = db.Column(db.Text, nullable=True)
    duration_minutes     = db.Column(db.Float, nullable=True)
    is_on_time           = db.Column(db.Boolean, nullable=True)
    # status: active | completed | cancelled
    status               = db.Column(db.String(20), default="active")
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.Index("idx_dt_emp_date", "emp_id", "trip_date"),
    )


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

        # ─────────────────────────────────────────────────────────────
        # NEW HARD-LEVEL FORMULAS (Phase 1)
        # ─────────────────────────────────────────────────────────────
        # Hourly targets (admin can override per-employee via custom_hourly_target)
        PICKER_TARGET_HR  = 50.0   # items/hr (per spec: Picker Target 50/hr)
        CHECKER_TARGET_HR = 70.0   # items/hr (per spec: Checker Target 70/hr)
        # Check for per-employee override
        try:
            if emp_id is not None:
                _emp_for_target = db.session.get(Employee, emp_id)
                if _emp_for_target and _emp_for_target.custom_hourly_target:
                    if staff_type == "picker":
                        PICKER_TARGET_HR = float(_emp_for_target.custom_hourly_target)
                    elif staff_type == "checker":
                        CHECKER_TARGET_HR = float(_emp_for_target.custom_hourly_target)
        except Exception:
            pass
        # Efficiency ratio targets (hard level = 200 items / 50 bills)
        HARD_ITEMS_PER_DAY = 200.0
        HARD_BILLS_PER_DAY = 50.0

        # Volume-Weighted Clean Rate:
        #   VWCR = (correct/total) × (actual_vol / hourly_target) × 100
        # Capped at 120 per admin preference (allows some reward for above-target).
        def _vwcr(correct, total, actual_per_hour, target_per_hour, cap=120.0):
            if total <= 0: return 0.0
            acc_ratio  = correct / total
            vol_ratio  = actual_per_hour / max(target_per_hour, 0.01)
            return round(min(acc_ratio * vol_ratio * 100, cap), 1)

        # Cleaner Rate — item weighting:
        #   Cleaner = (Normal×100 + Sweep/Rack×10) / Total Volume
        # "Sweep/Rack" treated as a combined bonus pool (days completed × rack_score)
        def _cleaner_rate(normal_items, total_items, sweep_days, rack_avg):
            if total_items <= 0: return 0.0
            # rack_avg is 0-10 scale, sweep_days is integer count
            sweep_rack_pool = (sweep_days or 0) + (rack_avg or 0)
            top = (normal_items * 100) + (sweep_rack_pool * 10)
            return round(min(safe_div(top, max(total_items, 1)), 100.0), 1)

        # Efficiency Ratio — are they hitting hard-level workload?
        #   Hard Level = 200 items / 50 bills per day
        #   Returns ratio 0-1 where 1.0 = hitting both targets
        def _efficiency_ratio(items_per_day, bills_per_day):
            items_r = min(safe_div(items_per_day, HARD_ITEMS_PER_DAY), 1.0)
            bills_r = min(safe_div(bills_per_day, HARD_BILLS_PER_DAY), 1.0)
            # Both must be hit — use minimum (weakest link)
            return round(min(items_r, bills_r), 3)

        # Calculate VWCR for the role
        daily_vol_items   = safe_div(ti, days) if staff_type == "picker" else safe_div(tck_total, days)
        daily_vol_per_hr  = safe_div(daily_vol_items, 9.0)  # 9-hr workday
        if staff_type == "picker":
            vwcr = _vwcr(tp, ti, daily_vol_per_hr, PICKER_TARGET_HR)
        elif staff_type == "checker":
            vwcr = _vwcr(tck_normal, tck_total, daily_vol_per_hr, CHECKER_TARGET_HR)
        else:
            # Purchaser VWCR: racked items accuracy × volume vs target
            pur_items_daily = safe_div(tck_urgent, days)  # tck_urgent = pur_items for purchaser
            pur_items_per_hr = safe_div(pur_items_daily, 9.0)
            vwcr = _vwcr(tro, max(tck_urgent, 1), pur_items_per_hr, 30.0)  # purchaser lighter target

        # Cleaner Rate
        if staff_type == "picker":
            cleaner_rate_score = _cleaner_rate(tp, max(ti, 1), tsd, safe_div(tro, max(days, 1)))
        elif staff_type == "checker":
            cleaner_rate_score = _cleaner_rate(tck_normal, max(tck_total, 1), tsd, 0)
        else:
            cleaner_rate_score = _cleaner_rate(tro, max(tck_urgent, 1), tsd, 0)

        # Efficiency Ratio per day
        items_daily = safe_div(ti if staff_type == "picker" else tck_total, max(days, 1))
        bills_daily = safe_div(tsb, max(days, 1))
        efficiency_ratio = _efficiency_ratio(items_daily, bills_daily)
        is_efficient = efficiency_ratio >= 1.0

        # Workspace Score for ALL roles — rack (0-10)/2 + table Y/N ×3 + sweep Y/N ×2 → /10 ×100
        # Already computed as workspace_score above, but compute for checker too (was 0 before)
        if staff_type == "checker":
            # Checker workspace = table (3) + sweep (2) — no rack since they don't arrange racks
            workspace_score_checker = round((table_ws + sweep_ws) / 5 * 100, 1) if days > 0 else 0.0
            workspace_score = workspace_score_checker

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
            pur_pending_bills  = max(pur_bills_received - pur_bills_checked, 0)
            pur_cs_fulfilment  = round(min(safe_div(pur_cs_received, pur_cs_open) * 100, 100.0), 1) if pur_cs_open > 0 else 100.0
            pur_racking_eff    = round(safe_div(pur_items_racked, pur_items) * 100, 1) if pur_items > 0 else 0.0
            pur_speed          = round(safe_div(pur_items, ts_total), 1)
            pur_entry_rate     = round(min(safe_div(pur_bill_entry, max(pur_bills_received,1)), 1.0) * 100, 1)
            pur_pending_pct    = max(0.0, 1.0 - safe_div(pur_pending_bills, max(pur_bills_received,1)))

            # ── PURCHASER POINT SYSTEM (100 pts total) — Phase 1 rebalance ──
            eff_score = round(
                (pur_bill_rate     / 100) * 25 +  # was 28
                (pur_cs_fulfilment / 100) * 20 +  # was 23
                (pur_racking_eff   / 100) * 15 +  # was 18
                min(pur_speed      / 60,  1.0) * 10 +  # was 12
                (pur_entry_rate    / 100) * 8 +
                pur_pending_pct              * 5 +
                (vwcr              / 120) * 8 +   # NEW: VWCR (cap 120 → /120 to normalise)
                efficiency_ratio             * 4 +    # NEW: Efficiency Ratio
                (workspace_score   / 100) * 3 +   # was 4
                (consistency       / 100) * 2,    # NEW: Log Consistency
                1)

        elif staff_type == "picker":
            # ── PICKER POINT SYSTEM (100 pts total) — Phase 1 rebalance ──
            # OLD: Acc×25 + Fulfil×20 + Speed×18 + PackEff×12 + Workspace×10 + CS×8 + Consistency×7
            # NEW: VWCR×25 + Fulfil×18 + Speed×15 + EffRatio×10 + Workspace×10
            #    + CS×8 + Cleaner×7 + LogCons×7
            # (Packing Efficiency removed; 12 pts redistributed to VWCR+6, EffRatio+4, LogCons+2)
            eff_score = round(
                (vwcr            / 120) * 25 +    # NEW: VWCR (was Pick Accuracy×25)
                (bill_fulfilment / 100) * 18 +    # was 20
                min(pick_speed   / 250, 1.0) * 15 +  # was 18
                efficiency_ratio           * 10 + # NEW: Efficiency Ratio (200/50)
                (workspace_score / 100) * 10 +
                min(cs_fulfilment/ 100, 1.0) * 8 +
                (cleaner_rate_score / 100) * 7 + # NEW: Cleaner Rate (replaces some of packing)
                (consistency     / 100) * 7,     # Log Consistency
                1)
        else:
            # ── CHECKER POINT SYSTEM (100 pts total) — Phase 1 rebalance ──
            speed_score     = min(safe_div(check_speed, 80.0), 1.0) * 22  # was 30
            clearance_score = (clearance_rate / 100.0) * 22  # was 25
            vwcr_score      = (vwcr / 120.0) * 20  # NEW: VWCR replaces "Normal %"
            cleaner_score   = (cleaner_rate_score / 100.0) * 8  # NEW
            eff_ratio_score = efficiency_ratio * 8  # NEW: Efficiency Ratio
            consist_score   = (consistency / 100.0) * 8   # was 10 — Log Consistency
            poteff_score    = (potential_eff / 100.0) * 5  # was 8
            workspace_ck    = (workspace_score / 100.0) * 4  # NEW: Workspace for checker
            sweep_score     = safe_div(tsd, days) * 3  # was 2
            eff_score       = round(speed_score + clearance_score + vwcr_score +
                                    cleaner_score + eff_ratio_score + consist_score +
                                    poteff_score + workspace_ck + sweep_score, 1)
        # Apply complaint deductions (minus marking)
        complaint_deduction = get_complaint_deduction(staff_type, emp_id=emp_id)
        # Apply ongoing employee admin adjustment
        try:
            _emp_adj = 0.0
            if emp_id is not None:
                _emp = db.session.get(Employee, emp_id)
                if _emp and _emp.admin_adjustment:
                    _emp_adj = float(_emp.admin_adjustment or 0)
        except Exception:
            _emp_adj = 0.0
        # Apply per-entry admin adjustments (summed across entries in range)
        per_entry_adj = sum(float(e.admin_adjustment or 0) for e in entries if hasattr(e, 'admin_adjustment'))
        total_adjustment = _emp_adj + per_entry_adj

        eff_score = max(round(eff_score - complaint_deduction + total_adjustment, 1), 0.0)
        eff_score = min(eff_score, 100.0)

        # Auto-suggest: if error rate stays <1% over enough days, suggest raising the target
        auto_suggest = None
        if days >= 7 and ti > 100:  # enough signal
            err_rate = safe_div(tm, ti) * 100 if staff_type == "picker" else urgent_pct
            if err_rate < 1.0 and daily_vol_per_hr > 0:
                current_target = PICKER_TARGET_HR if staff_type == "picker" else CHECKER_TARGET_HR
                if daily_vol_per_hr > current_target * 0.9:
                    auto_suggest = f"Error rate under 1% for {days} days — consider raising hourly target from {int(current_target)} to {int(current_target * 1.4)}/hr."

        # Grade thresholds (unified across roles)

        # Unified grade thresholds
        if   eff_score >= 88: grade = "ELITE"
        elif eff_score >= 72: grade = "PROFICIENT"
        elif eff_score >= 52: grade = "SATISFACTORY"
        else:                 grade = "RE-TRAINING"

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
            admin_adjustment=round(total_adjustment, 1),
            vwcr=vwcr,
            cleaner_rate=cleaner_rate_score,
            efficiency_ratio=efficiency_ratio,
            is_efficient=is_efficient,
            items_daily=round(items_daily, 1),
            bills_daily=round(bills_daily, 1),
            hard_target_items=HARD_ITEMS_PER_DAY,
            hard_target_bills=HARD_BILLS_PER_DAY,
            hourly_target=(PICKER_TARGET_HR if staff_type == "picker"
                           else CHECKER_TARGET_HR if staff_type == "checker"
                           else 30.0),
            auto_suggest=auto_suggest,
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


def build_pdf_payload(emp: "Employee") -> Dict[str, Any]:
    """Build the dict passed to utils.generate_visual_pdf().

    Centralises the logic so /export_pdf, /download/<id> and /admin/export_all_pdf
    all produce identical data. Includes ranking, targeted complaints, and
    ordered (newest-first) entries.
    """
    try:
        # All entries, newest first (fixes the `all_entries[-30:]` bug where ordering
        # was unspecified and could yield arbitrary 30 rows)
        all_entries = (
            KPIEntry.query
            .filter_by(emp_id=emp.id)
            .order_by(KPIEntry.entry_date.desc())
            .all()
        )
        recent_30 = all_entries[:30]

        # Compute ranks (same logic as staff_detail)
        emp_overall_rank = None
        emp_role_rank = None
        try:
            all_staff = Employee.query.filter_by(is_admin=False).all()
            all_sc, role_sc = [], []
            for s in all_staff:
                s_ents = KPIEntry.query.filter_by(emp_id=s.id).all()
                s_stats = build_analytics(s_ents, s.staff_type, emp_id=s.id)
                if s_stats:
                    all_sc.append((s.id, s_stats["eff_score"]))
                    if s.staff_type == emp.staff_type:
                        role_sc.append((s.id, s_stats["eff_score"]))
            all_sc.sort(key=lambda x: x[1], reverse=True)
            role_sc.sort(key=lambda x: x[1], reverse=True)
            emp_overall_rank = next((i+1 for i,(sid,_) in enumerate(all_sc)  if sid==emp.id), None)
            emp_role_rank    = next((i+1 for i,(sid,_) in enumerate(role_sc) if sid==emp.id), None)
            total_staff = len(all_staff)
            total_role  = sum(1 for s in all_staff if s.staff_type == emp.staff_type)
        except Exception as re_err:
            logger.warning(f"PDF rank calc: {re_err}")
            total_staff = total_role = 0

        # Targeted complaints (pinned to this employee) + role-wide complaints
        complaints_targeted = []
        complaints_role = []
        try:
            unresolved = Complaint.query.filter_by(is_resolved=False).all()
            for c in unresolved:
                rec = {
                    "date": c.entry_date,
                    "type": c.complaint_type,
                    "description": c.description,
                    "picker_pts": c.picker_final or 0,
                    "checker_pts": c.checker_final or 0,
                    "purchaser_pts": c.purchaser_final or 0,
                    "target_emp_id": c.target_emp_id,
                }
                if c.target_emp_id == emp.id:
                    complaints_targeted.append(rec)
                elif c.target_emp_id is None:
                    complaints_role.append(rec)
        except Exception as ce:
            logger.warning(f"PDF complaint fetch: {ce}")

        # 30-day heatmap + sparkline data (newest → oldest, so chart reads left→right as time forward)
        today = date.today()
        heatmap = {}
        heatmap_errors = {}  # error-density map for Phase 3 PDF heat map
        heatmap_volume = {}  # volume map for Phase 3 PDF heat map
        for e in recent_30:
            delta = (today - e.entry_date).days
            if delta <= 29:
                if emp.staff_type == "checker":
                    tck_day = (e.checked or 0) + (e.errors_found or 0)
                    spd = round(tck_day / 9.0, 1)
                    heatmap[str(e.entry_date)] = min(round(spd / 50 * 100, 1), 100)
                    # Error density for checker = urgent items / total
                    err_total = (e.errors_found or 0)
                    total_items = tck_day or 1
                    heatmap_errors[str(e.entry_date)] = round(err_total / total_items * 100, 1)
                    heatmap_volume[str(e.entry_date)] = tck_day
                else:
                    heatmap[str(e.entry_date)] = e.accuracy
                    # Error density for picker = missed / total
                    total_picks = (e.picked or 0) + (e.missed or 0) or 1
                    heatmap_errors[str(e.entry_date)] = round((e.missed or 0) / total_picks * 100, 1)
                    heatmap_volume[str(e.entry_date)] = total_picks

        # Pending bill validations (Constant Protocol — Phase 3)
        pending_validations = []
        try:
            # Only pickers have their own submissions pending; for all staff show role-relevant ones
            if emp.staff_type == "picker":
                bvs = BillValidation.query.filter_by(
                    picker_id=emp.id
                ).filter(
                    BillValidation.status.in_(["pending", "mismatch"])
                ).order_by(BillValidation.entry_date.desc()).limit(20).all()
            else:
                # For checkers/purchasers, show any pending validations they might help with
                bvs = BillValidation.query.filter(
                    BillValidation.status == "pending"
                ).order_by(BillValidation.entry_date.desc()).limit(10).all()
            for bv in bvs:
                picker_emp = db.session.get(Employee, bv.picker_id)
                pending_validations.append({
                    "date": bv.entry_date,
                    "picker_name": picker_emp.name if picker_emp else f"#{bv.picker_id}",
                    "picker_count": bv.picker_count,
                    "slots_filled": bv.checker_count_submitted,
                    "status": bv.status,
                    "is_mine": bv.picker_id == emp.id,
                })
        except Exception as ve:
            logger.warning(f"PDF pending validations fetch: {ve}")

        return {
            "all_stats":    build_analytics(all_entries, emp.staff_type, emp_id=emp.id),
            "day_stats":    build_analytics(get_period_entries(emp.id, "day"),   emp.staff_type, emp_id=emp.id),
            "week_stats":   build_analytics(get_period_entries(emp.id, "week"),  emp.staff_type, emp_id=emp.id),
            "month_stats":  build_analytics(get_period_entries(emp.id, "month"), emp.staff_type, emp_id=emp.id),
            "staff_type":   emp.staff_type,
            "emp_email":    emp.email,
            "emp_role":     emp.role,
            "emp_role_rank":    emp_role_rank,
            "emp_overall_rank": emp_overall_rank,
            "total_role":       total_role,
            "total_staff":       total_staff,
            "complaints_targeted": complaints_targeted,
            "complaints_role":     complaints_role,
            "heatmap":      heatmap,
            "heatmap_errors": heatmap_errors,
            "heatmap_volume": heatmap_volume,
            "pending_validations": pending_validations,
            "all_entries":  recent_30,
        }
    except Exception as e:
        logger.error(f"build_pdf_payload: {e}")
        return {"staff_type": emp.staff_type, "all_entries": []}


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


def _safe_stats(stats):
    """Return a zero-filled stats dict when build_analytics returns None
    (staff with zero entries). Keeps templates safe to render."""
    if stats is None or not isinstance(stats, dict):
        return {
            "eff_score": 0.0, "grade": "NO DATA", "feedback": "No entries submitted yet.",
            "days": 0, "trend": "—", "consistency": 0,
            "complaint_deduction": 0, "admin_adjustment": 0,
            "vwcr": 0, "cleaner_rate": 0, "efficiency_ratio": 0, "is_efficient": False,
            "items_daily": 0, "bills_daily": 0,
            "hard_target_items": 200, "hard_target_bills": 50,
            "hourly_target": 50, "auto_suggest": None,
            "pick_acc": 0, "pick_speed": 0, "packing_eff": 0,
            "cs_fulfilment": 0, "workspace_score": 0, "bill_fulfilment": 0,
            "tp": 0, "tm": 0, "ti": 0, "tcs": 0, "tro": 0, "ttc": 0, "tsd": 0,
            "tsb": 0, "tpd": 0, "tbr_picker": 0, "potential_items": 0, "gap_items": 0,
            "ts": 0, "ttt": 0,
            "check_speed": 0, "ck_speed": 0, "clearance_rate": 100,
            "normal_pct": 0, "urgent_pct": 0, "potential_eff": 0,
            "tck_total": 0, "tck": 0, "tck_normal": 0, "tck_urgent": 0, "ter": 0,
            "tsb_normal": 0, "tsb_urgent": 0, "tsb_total": 0,
            "tbr": 0, "pending_bills": 0, "error_rate": 0, "check_acc": 0,
            "pur_bills_received": 0, "pur_bills_checked": 0, "pur_bill_entry": 0,
            "pur_items": 0, "pur_cs_open": 0, "pur_cs_received": 0,
            "pur_items_racked": 0, "pur_bill_rate": 0, "pur_cs_fulfilment": 0,
            "pur_racking_eff": 0, "pur_speed": 0, "pur_entry_rate": 0,
            "pur_pending_bills": 0,
        }
    return stats


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
    except Exception:
        db.session.rollback()


def send_sms(to_number: str, body: str) -> bool:
    """Send SMS via Twilio. Returns True on success. No-ops if env vars not set."""
    try:
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
        from_number = os.environ.get("TWILIO_FROM_NUMBER", "")
        if not (account_sid and auth_token and from_number):
            logger.info(f"SMS skipped (Twilio not configured): {body[:60]}")
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        resp = _requests.post(url, data={"From": from_number, "To": to_number, "Body": body},
                              auth=(account_sid, auth_token), timeout=10)
        if resp.status_code in (200, 201):
            logger.info(f"SMS sent to {to_number}")
            return True
        else:
            logger.warning(f"SMS failed ({resp.status_code}): {resp.text[:120]}")
            return False
    except Exception as e:
        logger.error(f"send_sms error: {e}")
        return False


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
                ("admin_adjustment","FLOAT DEFAULT 0"),
                ("admin_adjustment_note","VARCHAR(200) DEFAULT ''"),
                ("custom_hourly_target", "FLOAT"),
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
                ("admin_adjustment",    "FLOAT DEFAULT 0"),
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
            # Create bill_validations table (Constant Protocol — Phase 2)
            try:
                db.session.execute(db.text("""
                    CREATE TABLE IF NOT EXISTS bill_validations (
                        id SERIAL PRIMARY KEY,
                        entry_date DATE NOT NULL,
                        picker_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                        picker_count INTEGER NOT NULL,
                        checker1_id INTEGER REFERENCES employees(id),
                        checker1_count INTEGER,
                        checker1_at TIMESTAMP,
                        checker2_id INTEGER REFERENCES employees(id),
                        checker2_count INTEGER,
                        checker2_at TIMESTAMP,
                        checker3_id INTEGER REFERENCES employees(id),
                        checker3_count INTEGER,
                        checker3_at TIMESTAMP,
                        status VARCHAR(20) DEFAULT 'pending',
                        mismatch_emp_ids VARCHAR(100) DEFAULT '',
                        override_by INTEGER REFERENCES employees(id),
                        override_note VARCHAR(200) DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT _picker_date_uc UNIQUE (picker_id, entry_date)
                    )
                """))
                db.session.execute(db.text(
                    "CREATE INDEX IF NOT EXISTS idx_bv_date ON bill_validations(entry_date)"
                ))
                db.session.execute(db.text(
                    "CREATE INDEX IF NOT EXISTS idx_bv_status ON bill_validations(status)"
                ))
                db.session.commit()
                logger.info("✅ bill_validations table ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"bill_validations migration skipped: {ce}")
            # shift_note column on kpi_entries
            try:
                db.session.execute(db.text(
                    "ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS shift_note VARCHAR(300)"
                ))
                db.session.commit()
                logger.info("✅ kpi_entries.shift_note ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"kpi_entries.shift_note migration skipped: {ce}")
            # Add phone column to employees (Feature 15)
            try:
                db.session.execute(db.text(
                    "ALTER TABLE employees ADD COLUMN IF NOT EXISTS phone VARCHAR(20)"
                ))
                db.session.commit()
                logger.info("✅ employees.phone ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"employees.phone migration skipped: {ce}")
            # Secondary staff type
            try:
                db.session.execute(db.text(
                    "ALTER TABLE employees ADD COLUMN IF NOT EXISTS secondary_staff_type VARCHAR(20)"
                ))
                db.session.commit()
                logger.info("✅ employees.secondary_staff_type ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"employees.secondary_staff_type migration skipped: {ce}")
            # New feature tables
            for tbl_sql in [
                """CREATE TABLE IF NOT EXISTS announcements (
                    id SERIAL PRIMARY KEY, title VARCHAR(150) NOT NULL, body TEXT NOT NULL,
                    created_by INTEGER REFERENCES employees(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE, expires_at TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS announcement_reads (
                    id SERIAL PRIMARY KEY,
                    announcement_id INTEGER REFERENCES announcements(id) ON DELETE CASCADE,
                    emp_id INTEGER REFERENCES employees(id) ON DELETE CASCADE,
                    read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _ann_emp_uc UNIQUE(announcement_id, emp_id))""",
                """CREATE TABLE IF NOT EXISTS staff_goals (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    target_score FLOAT NOT NULL, month VARCHAR(7) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _goal_emp_month_uc UNIQUE(emp_id, month))""",
                """CREATE TABLE IF NOT EXISTS past_entry_requests (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    requested_date DATE NOT NULL, reason VARCHAR(200) NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending', admin_note VARCHAR(200),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS badges (
                    id SERIAL PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL,
                    description VARCHAR(200) NOT NULL, icon VARCHAR(10) NOT NULL,
                    badge_type VARCHAR(20) DEFAULT 'auto')""",
                """CREATE TABLE IF NOT EXISTS staff_badges (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    badge_id INTEGER NOT NULL REFERENCES badges(id) ON DELETE CASCADE,
                    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    awarded_by INTEGER REFERENCES employees(id),
                    note VARCHAR(200) DEFAULT '',
                    CONSTRAINT _staff_badge_uc UNIQUE(emp_id, badge_id))""",
                # Feature 14: Admin Staff Notes
                """CREATE TABLE IF NOT EXISTS admin_staff_notes (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    note TEXT NOT NULL,
                    created_by INTEGER NOT NULL REFERENCES employees(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
                # Feature 15: Password Reset OTP
                """CREATE TABLE IF NOT EXISTS password_reset_otps (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    otp_code VARCHAR(6) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    is_used BOOLEAN DEFAULT FALSE)""",
                # Feature 16: Monthly Report Archive
                """CREATE TABLE IF NOT EXISTS monthly_report_archives (
                    id SERIAL PRIMARY KEY,
                    month_str VARCHAR(7) UNIQUE NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    emp_count INTEGER DEFAULT 0,
                    file_path VARCHAR(200))""",
                # Multitask entries
                """CREATE TABLE IF NOT EXISTS multitask_entries (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    entry_date DATE NOT NULL,
                    secondary_type VARCHAR(20) NOT NULL,
                    sales_bills_open INTEGER DEFAULT 0,
                    picked INTEGER DEFAULT 0,
                    missed INTEGER DEFAULT 0,
                    checked INTEGER DEFAULT 0,
                    errors_found INTEGER DEFAULT 0,
                    packing_done INTEGER DEFAULT 0,
                    cs_sales_open INTEGER DEFAULT 0,
                    total_bills_received INTEGER DEFAULT 0,
                    bills_received INTEGER DEFAULT 0,
                    pending_bills_manual INTEGER DEFAULT 0,
                    quantity INTEGER DEFAULT 0,
                    note VARCHAR(300),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
                # Delivery / Biller notes
                """CREATE TABLE IF NOT EXISTS delivery_biller_notes (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    entry_date DATE NOT NULL,
                    note_type VARCHAR(20) NOT NULL,
                    note_text TEXT NOT NULL,
                    quantity INTEGER DEFAULT 0,
                    issues_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _dbnote_emp_date_uc UNIQUE(emp_id, entry_date))""",
            ]:
                try:
                    db.session.execute(db.text(tbl_sql))
                    db.session.commit()
                except Exception as te:
                    db.session.rollback()
                    logger.warning(f"New table migration skipped: {te}")
            # Delivery tracking tables
            for tbl_sql in [
                """CREATE TABLE IF NOT EXISTS delivery_assignments (
                    id SERIAL PRIMARY KEY,
                    purchaser_id INTEGER NOT NULL REFERENCES employees(id),
                    delivery_emp_id INTEGER NOT NULL REFERENCES employees(id),
                    destination_addr TEXT NOT NULL,
                    destination_lat FLOAT,
                    destination_lng FLOAT,
                    package_desc TEXT NOT NULL,
                    bills_count INTEGER DEFAULT 0,
                    recipient_name VARCHAR(150),
                    company_name VARCHAR(150),
                    status VARCHAR(20) DEFAULT 'pending',
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT)""",
                """CREATE TABLE IF NOT EXISTS delivery_trips (
                    id SERIAL PRIMARY KEY,
                    assignment_id INTEGER NOT NULL REFERENCES delivery_assignments(id) ON DELETE CASCADE,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    trip_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    departure_lat FLOAT,
                    departure_lng FLOAT,
                    departure_time TIMESTAMP,
                    arrival_time TIMESTAMP,
                    delivered_to_name VARCHAR(150),
                    delivered_to_company VARCHAR(150),
                    delivery_note TEXT,
                    duration_minutes FLOAT,
                    is_on_time BOOLEAN,
                    status VARCHAR(20) DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _dt_assignment_uc UNIQUE(assignment_id))""",
            ]:
                try:
                    db.session.execute(db.text(tbl_sql))
                    db.session.commit()
                except Exception as te:
                    db.session.rollback()
                    logger.warning(f"Delivery table migration skipped: {te}")
            # Add multitask KPI-parameter columns to an existing multitask_entries table
            for mt_col, mt_type in [
                ("sales_bills_open", "INTEGER DEFAULT 0"),
                ("picked", "INTEGER DEFAULT 0"),
                ("missed", "INTEGER DEFAULT 0"),
                ("checked", "INTEGER DEFAULT 0"),
                ("errors_found", "INTEGER DEFAULT 0"),
                ("packing_done", "INTEGER DEFAULT 0"),
                ("cs_sales_open", "INTEGER DEFAULT 0"),
                ("total_bills_received", "INTEGER DEFAULT 0"),
                ("bills_received", "INTEGER DEFAULT 0"),
                ("pending_bills_manual", "INTEGER DEFAULT 0"),
            ]:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE multitask_entries ADD COLUMN IF NOT EXISTS {mt_col} {mt_type}"
                    ))
                    db.session.commit()
                except Exception as mce:
                    db.session.rollback()
                    logger.warning(f"multitask_entries.{mt_col} migration skipped: {mce}")
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
                "admin_adjustment": "FLOAT DEFAULT 0",
                "admin_adjustment_note": "VARCHAR(200) DEFAULT ''",
                "custom_hourly_target": "FLOAT",
                "phone": "VARCHAR(20)",  # Feature 15
                "secondary_staff_type": "VARCHAR(20)",  # 2nd role for multitasking
            }
            for col, col_type in sqlite_cols.items():
                if col not in existing:
                    cursor.execute(f"ALTER TABLE employees ADD COLUMN {col} {col_type}")
                    logger.info(f"✅ SQLite column '{col}' added")
            # KPI entries sqlite
            try:
                cursor.execute("PRAGMA table_info(kpi_entries)")
                kpi_existing = [row[1] for row in cursor.fetchall()]
                if "admin_adjustment" not in kpi_existing:
                    cursor.execute("ALTER TABLE kpi_entries ADD COLUMN admin_adjustment FLOAT DEFAULT 0")
                    logger.info("✅ SQLite kpi_entries.admin_adjustment added")
            except Exception as kce:
                logger.warning(f"SQLite kpi_entries migration: {kce}")
            # complaints.target_emp_id
            try:
                cursor.execute("PRAGMA table_info(complaints)")
                cmp_cols = [row[1] for row in cursor.fetchall()]
                if cmp_cols and "target_emp_id" not in cmp_cols:
                    cursor.execute("ALTER TABLE complaints ADD COLUMN target_emp_id INTEGER")
                    logger.info("✅ SQLite complaints.target_emp_id added")
            except Exception as sce:
                logger.warning(f"SQLite complaints migration: {sce}")
            # shift_note on kpi_entries
            try:
                cursor.execute("PRAGMA table_info(kpi_entries)")
                kpi_cols2 = [row[1] for row in cursor.fetchall()]
                if "shift_note" not in kpi_cols2:
                    cursor.execute("ALTER TABLE kpi_entries ADD COLUMN shift_note VARCHAR(300)")
                    logger.info("✅ SQLite kpi_entries.shift_note added")
            except Exception as sne:
                logger.warning(f"SQLite kpi_entries.shift_note migration: {sne}")
            # Feature 14: admin_staff_notes
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS admin_staff_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    note TEXT NOT NULL,
                    created_by INTEGER NOT NULL REFERENCES employees(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                logger.info("✅ SQLite admin_staff_notes ensured")
            except Exception as asne:
                logger.warning(f"SQLite admin_staff_notes: {asne}")
            # Feature 15: password_reset_otps
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS password_reset_otps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    otp_code VARCHAR(6) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    is_used BOOLEAN DEFAULT 0)""")
                logger.info("✅ SQLite password_reset_otps ensured")
            except Exception as prote:
                logger.warning(f"SQLite password_reset_otps: {prote}")
            # Feature 16: monthly_report_archives
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS monthly_report_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month_str VARCHAR(7) UNIQUE NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    emp_count INTEGER DEFAULT 0,
                    file_path VARCHAR(200))""")
                logger.info("✅ SQLite monthly_report_archives ensured")
            except Exception as mrae:
                logger.warning(f"SQLite monthly_report_archives: {mrae}")
            # Multitask entries
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS multitask_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    entry_date DATE NOT NULL,
                    secondary_type VARCHAR(20) NOT NULL,
                    sales_bills_open INTEGER DEFAULT 0,
                    picked INTEGER DEFAULT 0,
                    missed INTEGER DEFAULT 0,
                    checked INTEGER DEFAULT 0,
                    errors_found INTEGER DEFAULT 0,
                    packing_done INTEGER DEFAULT 0,
                    cs_sales_open INTEGER DEFAULT 0,
                    total_bills_received INTEGER DEFAULT 0,
                    bills_received INTEGER DEFAULT 0,
                    pending_bills_manual INTEGER DEFAULT 0,
                    quantity INTEGER DEFAULT 0,
                    note VARCHAR(300),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                # Add columns to an existing table (older deploys)
                cursor.execute("PRAGMA table_info(multitask_entries)")
                mt_existing = [row[1] for row in cursor.fetchall()]
                for mt_col in ["sales_bills_open", "picked", "missed", "checked",
                               "errors_found", "packing_done", "cs_sales_open",
                               "total_bills_received", "bills_received", "pending_bills_manual"]:
                    if mt_col not in mt_existing:
                        cursor.execute(f"ALTER TABLE multitask_entries ADD COLUMN {mt_col} INTEGER DEFAULT 0")
                logger.info("✅ SQLite multitask_entries ensured")
            except Exception as mte:
                logger.warning(f"SQLite multitask_entries: {mte}")
            # Delivery / Biller notes
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS delivery_biller_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    entry_date DATE NOT NULL,
                    note_type VARCHAR(20) NOT NULL,
                    note_text TEXT NOT NULL,
                    quantity INTEGER DEFAULT 0,
                    issues_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _dbnote_emp_date_uc UNIQUE(emp_id, entry_date))""")
                logger.info("✅ SQLite delivery_biller_notes ensured")
            except Exception as dbne:
                logger.warning(f"SQLite delivery_biller_notes: {dbne}")
            # SQLite: delivery_assignments and delivery_trips
            try:
                cursor.execute("""CREATE TABLE IF NOT EXISTS delivery_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchaser_id INTEGER NOT NULL,
                    delivery_emp_id INTEGER NOT NULL,
                    destination_addr TEXT NOT NULL,
                    destination_lat REAL,
                    destination_lng REAL,
                    package_desc TEXT NOT NULL,
                    bills_count INTEGER DEFAULT 0,
                    recipient_name VARCHAR(150),
                    company_name VARCHAR(150),
                    status VARCHAR(20) DEFAULT 'pending',
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT)""")
                cursor.execute("""CREATE TABLE IF NOT EXISTS delivery_trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    emp_id INTEGER NOT NULL,
                    trip_date DATE NOT NULL,
                    departure_lat REAL,
                    departure_lng REAL,
                    departure_time TIMESTAMP,
                    arrival_time TIMESTAMP,
                    delivered_to_name VARCHAR(150),
                    delivered_to_company VARCHAR(150),
                    delivery_note TEXT,
                    duration_minutes REAL,
                    is_on_time INTEGER,
                    status VARCHAR(20) DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(assignment_id))""")
                conn.commit()
                logger.info("✅ SQLite delivery tables created")
            except Exception as dte:
                logger.warning(f"SQLite delivery tables: {dte}")
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
                _admin_pw = os.environ.get("ADMIN_PASSWORD", "admin123")
                if _admin_pw == "admin123":
                    logger.warning("⚠️ Using default admin password 'admin123'. Set ADMIN_PASSWORD env var for production.")
                admin = Employee(name="Admin", email=os.environ.get("ADMIN_EMAIL", "admin@pharmaip.com"), staff_type="picker", is_admin=True, role="Admin")
                admin.set_password(_admin_pw)
                db.session.add(admin)
                p1 = Employee(name="Rahul Sharma", email="rahul@pharmaip.com", staff_type="picker", role="Picker")
                p1.set_password("test1234")
                db.session.add(p1)
                c1 = Employee(name="Priya Patel", email="priya@pharmaip.com", staff_type="checker", role="Checker")
                c1.set_password("test1234")
                db.session.add(c1)
                db.session.commit()
                logger.info("✅ Database initialized successfully")
            # Seed default auto badges if none exist
            try:
                if Badge.query.count() == 0:
                    default_badges = [
                        Badge(name="Perfect Week", description="100% accuracy for 5+ consecutive days", icon="🎯", badge_type="auto"),
                        Badge(name="Speed Demon", description="Pick speed > 60 items/hr average this month", icon="⚡", badge_type="auto"),
                        Badge(name="Clean Sweep", description="100% workspace score for full week", icon="🧹", badge_type="auto"),
                        Badge(name="Century Club", description="100+ items picked in a single day", icon="📦", badge_type="auto"),
                        Badge(name="Top Performer", description="#1 score in role for the month", icon="🏆", badge_type="auto"),
                        Badge(name="7-Day Streak", description="Submitted every day for 7 consecutive days", icon="🔥", badge_type="auto"),
                        Badge(name="First Delivery", description="Completed your very first GPS delivery", icon="🚚", badge_type="auto"),
                        Badge(name="Speed Rider", description="Completed 3 deliveries faster than the average time", icon="⚡", badge_type="auto"),
                        Badge(name="Perfect Courier", description="10 consecutive on-time deliveries", icon="🎯", badge_type="auto"),
                        Badge(name="Veteran Courier", description="50 total deliveries completed", icon="🏆", badge_type="auto"),
                    ]
                    for b in default_badges:
                        db.session.add(b)
                    db.session.commit()
                    logger.info("✅ Default badges seeded")
            except Exception as badge_err:
                db.session.rollback()
                logger.warning(f"Badge seeding: {badge_err}")
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
    except Exception:
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
            session["primary_staff_type"] = user.staff_type
            session["secondary_staff_type"] = user.secondary_staff_type
            session["is_admin"] = bool(user.is_admin)

            logger.info(f"User login successful: {email}")
            if user.is_admin:
                return redirect(url_for("admin_dashboard"))
            # If staff has a secondary role, let them choose which role to work as today
            if user.secondary_staff_type and user.secondary_staff_type.strip():
                return redirect(url_for("select_role"))
            return redirect(url_for("dashboard"))
        except Exception as e:
            logger.error(f"Login error: {e}")
            flash("System error. Please try again.", "danger")
    return render_template("login.html")


@app.route("/select_role", methods=["GET"])
@login_required
def select_role():
    """After login, multitasking staff choose which role to work as today."""
    primary = session.get("primary_staff_type") or session.get("staff_type", "picker")
    secondary = session.get("secondary_staff_type")
    if not secondary:
        return redirect(url_for("dashboard"))
    return render_template("select_role.html",
        user_name=session.get("user_name", "User"),
        primary_role=primary,
        secondary_role=secondary,
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/switch_role", methods=["POST"])
@login_required
def switch_role():
    """Toggle session staff_type between primary and secondary role (multitasking)."""
    target = request.form.get("role", "").strip()
    primary = session.get("primary_staff_type")
    secondary = session.get("secondary_staff_type")
    # Re-fetch from DB if session is missing values (older session pre-feature)
    if not primary:
        emp = db.session.get(Employee, session.get("user_id"))
        if emp:
            primary = emp.staff_type
            secondary = emp.secondary_staff_type
            session["primary_staff_type"] = primary
            session["secondary_staff_type"] = secondary
    if target in (primary, secondary) and target:
        session["staff_type"] = target
        flash(f"Now working as {target.title()}.", "success")
    else:
        flash("Invalid role switch.", "warning")
    return redirect(url_for("dashboard"))


def _save_multitask_entry(emp_id, entry_date, primary_role):
    """Save (or update) the staff member's multitask Role-2 report for the day.
    Reads per-role mt_* fields from the request form. The chosen role can differ
    each day. No-ops cleanly if nothing meaningful was entered."""
    role = (request.form.get("multitask_role", "") or "").strip()
    if not role or role == primary_role:
        return
    def mi(k):
        try: return max(0, int(request.form.get(k, 0) or 0))
        except Exception: return 0
    note = (request.form.get("multitask_note", "") or "").strip()[:300]

    # Map per-role form fields → unified MultitaskEntry columns
    vals = dict(sales_bills_open=0, picked=0, missed=0, checked=0, errors_found=0,
                packing_done=0, cs_sales_open=0, total_bills_received=0,
                bills_received=0, pending_bills_manual=0, quantity=0)
    if role == "picker":
        vals.update(total_bills_received=mi("mt_p_total_bills"),
                    sales_bills_open=mi("mt_p_sbo"), picked=mi("mt_p_picked"),
                    missed=mi("mt_p_missed"), cs_sales_open=mi("mt_p_cso"),
                    packing_done=mi("mt_p_packing"))
    elif role == "checker":
        vals.update(bills_received=mi("mt_c_bills_received"),
                    pending_bills_manual=mi("mt_c_pending"),
                    sales_bills_open=mi("mt_c_sbo"), cs_sales_open=mi("mt_c_cso"),
                    checked=mi("mt_c_checked"), errors_found=mi("mt_c_errors"))
    elif role == "purchaser":
        vals.update(sales_bills_open=mi("mt_pu_sbo"), checked=mi("mt_pu_checked"),
                    picked=mi("mt_pu_picked"), errors_found=mi("mt_pu_items"),
                    cs_sales_open=mi("mt_pu_cso"), packing_done=mi("mt_pu_packing"))
    else:  # delivery / billing / packing / other
        vals.update(quantity=mi("mt_s_quantity"))

    # Skip if absolutely nothing was filled in
    if not any(vals.values()) and not note:
        return

    # Upsert on (emp, date, role) so re-submitting the same role updates it
    mt = MultitaskEntry.query.filter_by(
        emp_id=emp_id, entry_date=entry_date, secondary_type=role
    ).first()
    if not mt:
        mt = MultitaskEntry(emp_id=emp_id, entry_date=entry_date, secondary_type=role)
        db.session.add(mt)
    for k, v in vals.items():
        setattr(mt, k, v)
    mt.note = note or None
    db.session.commit()


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    try:
        emp_id = session.get("user_id")
        staff_type = session.get("staff_type", "picker")
        today = date.today()
        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()
        new_personal_best = False

        # ── Delivery / Biller: simple note entry (no KPIEntry) ──────────────────
        today_db_note = None
        if staff_type in ("delivery", "biller"):
            today_db_note = DeliveryBillerNote.query.filter_by(emp_id=emp_id, entry_date=today).first()
            if request.method == "POST" and not today_db_note:
                note_text = request.form.get("delivery_note", "").strip()
                if note_text:
                    try:
                        db_note = DeliveryBillerNote(
                            emp_id=emp_id,
                            entry_date=today,
                            note_type=staff_type,
                            note_text=note_text[:1000],
                            quantity=max(0, int(request.form.get("delivery_qty", 0) or 0)),
                            issues_count=max(0, int(request.form.get("delivery_issues", 0) or 0)),
                        )
                        db.session.add(db_note)
                        db.session.commit()
                        today_db_note = db_note
                        flash("✅ Daily note recorded successfully.", "success")
                    except Exception as dbe:
                        db.session.rollback()
                        logger.error(f"DeliveryBillerNote save: {dbe}")
                        flash("Error saving note.", "danger")
            return render_template("dashboard.html",
                user_name=session.get("user_name", "User"),
                user_id=emp_id,
                staff_type=staff_type,
                today=today,
                today_entry=None,
                today_db_note=today_db_note,
                d_stats=None, w_stats=None, m_stats=None,
                recent=DeliveryBillerNote.query.filter_by(emp_id=emp_id).order_by(
                    DeliveryBillerNote.entry_date.desc()).limit(14).all(),
                trend_labels=[], trend_accuracy=[], trend_speed=[],
                total_entries=DeliveryBillerNote.query.filter_by(emp_id=emp_id).count(),
                new_personal_best=False, pending_windows=[], active_announcements=[],
                unread_count=0, current_goal=None, pending_requests=[],
                my_staff_badges=[], all_auto_badges=[], earned_badge_ids=set(),
                current_month=today.strftime("%B %Y"), month_str=today.strftime("%Y-%m"),
                today_multitask=[],
                primary_staff_type=session.get("primary_staff_type") or staff_type,
                secondary_staff_type=session.get("secondary_staff_type"),
            )

        # ── Independent multitask submission (Role 2 can be filled/changed any time) ──
        if request.method == "POST" and request.form.get("multitask_only") == "1":
            try:
                _save_multitask_entry(emp_id, today, staff_type)
                flash("✅ Multitask role report saved.", "success")
            except Exception as mte:
                db.session.rollback()
                logger.warning(f"Independent multitask save: {mte}")
                flash("Error saving multitask report.", "danger")
            return redirect(url_for("dashboard"))

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

                    # ── VALIDATION: reject submission if required fields missing ──
                    missing_fields = []
                    if staff_type == "picker":
                        if total_bills_received <= 0:
                            missing_fields.append("Total Bills Received (TB Sales)")
                        if picked <= 0:
                            missing_fields.append("Items Picked")
                    elif staff_type == "checker":
                        if bills_received <= 0:
                            missing_fields.append("Bills Received (TB Sales)")
                        if checked <= 0:
                            missing_fields.append("Items Checked")
                    elif staff_type == "purchaser":
                        if sales_bills_open <= 0:
                            missing_fields.append("PO Bills Received (TB Sales)")
                        if errors_found <= 0:
                            missing_fields.append("Number of Items")

                    if missing_fields:
                        flash(f"❌ Cannot save — required fields missing or zero: {', '.join(missing_fields)}.", "danger")
                        return redirect(url_for("dashboard"))

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

                    # ── Constant Protocol: create pending BillValidation for pickers
                    if staff_type == "picker" and total_bills_received > 0:
                        try:
                            bv = BillValidation.query.filter_by(
                                picker_id=emp_id, entry_date=today
                            ).first()
                            if not bv:
                                bv = BillValidation(
                                    picker_id=emp_id,
                                    entry_date=today,
                                    picker_count=total_bills_received,
                                    status="pending",
                                )
                                db.session.add(bv)
                                db.session.commit()
                                logger.info(f"Bill validation created: picker {emp_id}, count {total_bills_received}")
                        except Exception as bve:
                            db.session.rollback()
                            logger.error(f"BillValidation create failed: {bve}")

                    all_entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
                    all_stats = build_analytics(all_entries, staff_type, emp_id=emp_id)
                    if all_stats:
                        prev_best = session.get(f"pb_{emp_id}", 0)
                        if all_stats['eff_score'] > prev_best:
                            session[f"pb_{emp_id}"] = all_stats['eff_score']
                            new_personal_best = True

                    # Save shift note (Feature 1)
                    try:
                        shift_note = request.form.get("shift_note", "").strip()[:300]
                        if shift_note:
                            ne.shift_note = shift_note
                            db.session.commit()
                    except Exception:
                        pass

                    flash("✅ Metrics recorded successfully.", "success")
                    today_entry = ne

                    # ── Multitask: save secondary role work with its own params ──
                    try:
                        _save_multitask_entry(emp_id, today, staff_type)
                    except Exception as mte:
                        db.session.rollback()
                        logger.warning(f"MultitaskEntry save: {mte}")

                    # Live broadcast — Last Entry update for admin dashboard
                    socketio.emit('entry_update', {
                        'user': session.get('user_name'),
                        'user_id': emp_id,
                        'staff_type': staff_type,
                        'accuracy': ne.accuracy,
                        'last_entry': today.strftime('%Y-%m-%d'),
                        'last_entry_time': datetime.utcnow().strftime('%H:%M'),
                    })

                    # Feature 5: Compute auto badges (never breaks main flow)
                    try:
                        compute_auto_badges(emp_id)
                    except Exception:
                        pass

                    # Feature 11: SMS alert if low KPI score today
                    try:
                        day_s = build_analytics(get_period_entries(emp_id, "day"), staff_type, emp_id=emp_id)
                        if day_s and day_s.get("eff_score", 100) < 50:
                            admin_phone = os.environ.get("ADMIN_PHONE", "")
                            emp_name = session.get("user_name", f"emp#{emp_id}")
                            score = day_s["eff_score"]
                            send_sms(admin_phone, f"Low KPI alert: {emp_name} scored {score}pts today.")
                    except Exception:
                        pass

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

        # Feature 2: Announcements — active, not expired, not dismissed
        try:
            now = datetime.utcnow()
            anns = Announcement.query.filter_by(is_active=True).all()
            dismissed_ids = set(
                ar.announcement_id for ar in AnnouncementRead.query.filter_by(emp_id=emp_id).all()
            )
            active_announcements = [
                a for a in anns
                if a.id not in dismissed_ids and (a.expires_at is None or a.expires_at > now)
            ]
            unread_count = len(active_announcements)
        except Exception:
            active_announcements = []
            unread_count = 0

        # Feature 3: Personal goal for this month
        try:
            month_str = today.strftime("%Y-%m")
            current_goal = StaffGoal.query.filter_by(emp_id=emp_id, month=month_str).first()
        except Exception:
            current_goal = None

        # Feature 4: Past entry requests (pending)
        try:
            pending_requests = PastEntryRequest.query.filter_by(emp_id=emp_id, status="pending").all()
        except Exception:
            pending_requests = []

        # Feature 5: Badges
        try:
            my_staff_badges = (
                db.session.query(StaffBadge, Badge)
                .join(Badge, StaffBadge.badge_id == Badge.id)
                .filter(StaffBadge.emp_id == emp_id)
                .all()
            )
            all_auto_badges = Badge.query.filter_by(badge_type="auto").all()
            earned_badge_ids = {sb.badge_id for sb, _ in my_staff_badges}
        except Exception:
            my_staff_badges = []
            all_auto_badges = []
            earned_badge_ids = set()

        # Multitask entries for today
        try:
            today_multitask = MultitaskEntry.query.filter_by(
                emp_id=emp_id, entry_date=today
            ).order_by(MultitaskEntry.created_at.desc()).all()
        except Exception:
            today_multitask = []

        # Multitasker bonus: +15 to daily score when secondary role work was also submitted today
        if today_multitask and d_stats:
            d_stats = dict(d_stats)
            d_stats["eff_score"] = min(round(d_stats["eff_score"] + 15, 1), 100.0)
            d_stats["multitask_bonus"] = 15

        # Delivery module data
        try:
            my_assignments = []
            purchaser_delivery_staff = []
            my_deliveries_today = []
            trip_map = {}
            if staff_type == "delivery":
                my_assignments = (
                    DeliveryAssignment.query
                    .filter(
                        DeliveryAssignment.delivery_emp_id == emp_id,
                        DeliveryAssignment.status.in_(["pending", "in_transit"])
                    )
                    .order_by(DeliveryAssignment.assigned_at.desc())
                    .all()
                )
                my_deliveries_today = (
                    DeliveryTrip.query
                    .filter_by(emp_id=emp_id, trip_date=today, status="completed")
                    .all()
                )
            elif staff_type == "biller" or session.get("is_admin"):
                try:
                    purchaser_delivery_staff = Employee.query.filter(
                        Employee.is_admin == False,
                        or_(Employee.staff_type == "delivery",
                            Employee.secondary_staff_type == "delivery")
                    ).all()
                except Exception as _qe:
                    logger.warning(f"delivery staff or_() query failed ({_qe}), falling back to primary only")
                    purchaser_delivery_staff = Employee.query.filter(
                        Employee.is_admin == False,
                        Employee.staff_type == "delivery"
                    ).all()
            if my_assignments:
                aid_list = [a.id for a in my_assignments]
                trips = DeliveryTrip.query.filter(DeliveryTrip.assignment_id.in_(aid_list)).all()
                trip_map = {t.assignment_id: t for t in trips}
        except Exception as _de:
            logger.error(f"Delivery module data load failed: {_de}")
            my_assignments = []
            purchaser_delivery_staff = []
            my_deliveries_today = []
            trip_map = {}

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
            pending_windows=pending_windows,
            active_announcements=active_announcements,
            unread_count=unread_count,
            current_goal=current_goal,
            pending_requests=pending_requests,
            my_staff_badges=my_staff_badges,
            all_auto_badges=all_auto_badges,
            earned_badge_ids=earned_badge_ids,
            current_month=today.strftime("%B %Y"),
            month_str=today.strftime("%Y-%m"),
            today_multitask=today_multitask,
            today_db_note=None,
            primary_staff_type=session.get("primary_staff_type") or staff_type,
            secondary_staff_type=session.get("secondary_staff_type"),
            my_assignments=my_assignments,
            purchaser_delivery_staff=purchaser_delivery_staff,
            my_deliveries_today=my_deliveries_today,
            trip_map=trip_map,
            store_lat=STORE_LAT,
            store_lng=STORE_LNG,
            tomtom_key=TOMTOM_API_KEY,
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return render_template("dashboard.html",
            user_name="User", user_id=0, staff_type="picker", today=date.today(),
            today_entry=None, d_stats=None, w_stats=None, m_stats=None,
            recent=[], trend_labels=[], trend_accuracy=[], trend_speed=[], total_entries=0,
            new_personal_best=False, pending_windows=[],
            active_announcements=[], unread_count=0, current_goal=None,
            pending_requests=[], my_staff_badges=[], all_auto_badges=[],
            earned_badge_ids=set(), current_month="", month_str="",
            today_multitask=[], today_db_note=None,
            primary_staff_type="picker", secondary_staff_type=None,
            my_assignments=[], purchaser_delivery_staff=[], my_deliveries_today=[],
            trip_map={}, store_lat=0.0, store_lng=0.0, tomtom_key="")


@app.route("/admin_dashboard")
@admin_required
def admin_dashboard():
    try:
        employees = Employee.query.filter_by(is_admin=False).options(db.joinedload(Employee.entries)).all()
        rows = []
        today = date.today()
        month_start = today.replace(day=1)
        for emp in employees:
            ents = emp.entries
            month_ents = [e for e in ents if e.entry_date >= month_start]
            stats = build_analytics(month_ents, emp.staff_type, emp_id=emp.id)
            week_ents = [e for e in ents if e.entry_date >= today - timedelta(days=6)]
            week_stats = build_analytics(week_ents, emp.staff_type, emp_id=emp.id)
            rows.append({
                'emp': emp,
                'stats': stats,
                'week_stats': week_stats,
                'count': len(ents),
                'last_entry': max((e.entry_date for e in ents), default=None),
            })

        all_ents = KPIEntry.query.all()
        total_picked = sum(e.picked or 0 for e in all_ents)
        total_entries = len(all_ents)
        active_today = KPIEntry.query.filter_by(entry_date=today).count()
        open_windows = PastEntryWindow.query.filter_by(is_active=True).order_by(PastEntryWindow.past_date.desc()).all()
        open_complaints = Complaint.query.filter_by(is_resolved=False).order_by(Complaint.created_at.desc()).all()

        # Feature 17: Stale validations (pending > 24h)
        stale_cutoff = datetime.utcnow() - timedelta(hours=24)
        stale_validations_count = BillValidation.query.filter(
            BillValidation.status == 'pending',
            BillValidation.created_at <= stale_cutoff
        ).count()

        # Feature 14: Admin notes grouped by emp_id
        all_admin_notes = AdminStaffNote.query.order_by(AdminStaffNote.created_at.desc()).all()
        notes_by_emp = {}
        for n in all_admin_notes:
            notes_by_emp.setdefault(n.emp_id, []).append(n)

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

        # Feature 2: Announcements for admin
        try:
            all_announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
            total_staff_count = len(employees)
            ann_read_counts = {}
            for ann in all_announcements:
                ann_read_counts[ann.id] = AnnouncementRead.query.filter_by(announcement_id=ann.id).count()
        except Exception:
            all_announcements = []
            ann_read_counts = {}
            total_staff_count = len(employees)

        # Feature 4: Pending past entry requests
        try:
            pending_past_requests = PastEntryRequest.query.filter_by(status="pending").order_by(PastEntryRequest.created_at.desc()).all()
            # emp_map: id -> Employee (all employees incl. delivery/biller)
            _all_emp = Employee.query.all()
            emp_map = {e.id: e for e in _all_emp}
        except Exception:
            pending_past_requests = []
            emp_map = {}

        # Delivery / Biller notes (recent 30 days)
        try:
            cutoff_30 = today - timedelta(days=30)
            recent_db_notes = (
                DeliveryBillerNote.query
                .filter(DeliveryBillerNote.entry_date >= cutoff_30)
                .order_by(DeliveryBillerNote.entry_date.desc(), DeliveryBillerNote.created_at.desc())
                .all()
            )
        except Exception:
            recent_db_notes = []

        # Multitask (Role 2) reports — recent 30 days
        try:
            cutoff_mt = today - timedelta(days=30)
            recent_multitask = (
                MultitaskEntry.query
                .filter(MultitaskEntry.entry_date >= cutoff_mt)
                .order_by(MultitaskEntry.entry_date.desc(), MultitaskEntry.created_at.desc())
                .all()
            )
        except Exception:
            recent_multitask = []

        # Feature 5: Badges for admin
        try:
            all_badges = Badge.query.all()
        except Exception:
            all_badges = []

        return render_template("admin.html",
            rows=rows, total_picked=total_picked, total_entries=total_entries,
            active_today=active_today, emp_count=len(employees), today=today,
            current_month=today.strftime("%B %Y"),
            open_windows=open_windows,
            open_complaints=open_complaints,
            all_staff=employees,
            picker_lb=picker_lb, checker_lb=checker_lb, purchaser_lb=purchaser_lb, mixed_lb=mixed_lb,
            picker_week_lb=picker_week_lb, checker_week_lb=checker_week_lb, purchaser_week_lb=purchaser_week_lb, mixed_week_lb=mixed_week_lb,
            team_avg_eff=team_avg_eff, team_avg_acc=team_avg_acc,
            grade_counts=grade_counts, needs_attention=needs_attention, improving=improving,
            stale_validations_count=stale_validations_count,
            notes_by_emp=notes_by_emp,
            all_announcements=all_announcements,
            ann_read_counts=ann_read_counts,
            total_staff_count=total_staff_count,
            pending_past_requests=pending_past_requests,
            emp_map=emp_map,
            all_badges=all_badges,
            recent_db_notes=recent_db_notes,
            recent_multitask=recent_multitask,
        )
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        flash("Error loading admin dashboard.", "danger")
        return render_template("admin.html", rows=[], total_picked=0,
                               total_entries=0, active_today=0, emp_count=0, today=date.today(),
                               current_month=date.today().strftime("%B %Y"),
                               open_windows=[], open_complaints=[], all_staff=[],
                               picker_lb=[], checker_lb=[], purchaser_lb=[], mixed_lb=[],
                               picker_week_lb=[], checker_week_lb=[], purchaser_week_lb=[], mixed_week_lb=[],
                               team_avg_eff=0, team_avg_acc=0, improving=[],
                               grade_counts={"ELITE":0,"PROFICIENT":0,"SATISFACTORY":0,"RE-TRAINING":0},
                               needs_attention=[], stale_validations_count=0, notes_by_emp={},
                               all_announcements=[], ann_read_counts={}, total_staff_count=0,
                               pending_past_requests=[], emp_map={}, all_badges=[],
                               recent_db_notes=[], recent_multitask=[])


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

        # Guard against None stats (staff with zero entries) to keep template safe
        a_stats = _safe_stats(build_analytics(entries, emp.staff_type, emp_id=emp.id))
        d_stats = _safe_stats(build_analytics(get_period_entries(emp_id, "day"), emp.staff_type, emp_id=emp.id))
        w_stats = _safe_stats(build_analytics(get_period_entries(emp_id, "week"), emp.staff_type, emp_id=emp.id))
        m_stats = _safe_stats(build_analytics(get_period_entries(emp_id, "month"), emp.staff_type, emp_id=emp.id))

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

        # Feature 8: Month-over-Month delta
        mom_delta = 0.0
        prev_month_score = None
        try:
            cur_month_start = today.replace(day=1)
            prev_month_end = cur_month_start - timedelta(days=1)
            prev_month_start = prev_month_end.replace(day=1)
            cur_month_ents = [e for e in entries if e.entry_date >= cur_month_start]
            prev_month_ents = [e for e in entries if prev_month_start <= e.entry_date <= prev_month_end]
            cur_m_stats = build_analytics(cur_month_ents, emp.staff_type, emp_id=emp.id)
            prev_m_stats = build_analytics(prev_month_ents, emp.staff_type, emp_id=emp.id)
            if cur_m_stats and prev_m_stats:
                mom_delta = round(cur_m_stats["eff_score"] - prev_m_stats["eff_score"], 1)
                prev_month_score = prev_m_stats["eff_score"]
            elif cur_m_stats and not prev_m_stats:
                mom_delta = 0.0
        except Exception as mom_err:
            logger.warning(f"MoM delta: {mom_err}")

        # Feature 9: Last 6 months scorecard
        monthly_scores = []
        try:
            for i in range(5, -1, -1):
                ref = today.replace(day=1) - timedelta(days=1)
                for _ in range(i):
                    ref = ref.replace(day=1) - timedelta(days=1)
                m_start = ref.replace(day=1)
                m_end = ref
                m_label = m_start.strftime("%b %Y")
                m_ents = [e for e in entries if m_start <= e.entry_date <= m_end]
                if m_ents:
                    ms = build_analytics(m_ents, emp.staff_type, emp_id=emp.id)
                    monthly_scores.append({"month": m_label, "score": ms["eff_score"] if ms else None})
                else:
                    monthly_scores.append({"month": m_label, "score": None})
        except Exception as ms_err:
            logger.warning(f"6-month scores: {ms_err}")

        # Feature 5: Badges for staff detail
        try:
            emp_badges = (
                db.session.query(StaffBadge, Badge)
                .join(Badge, StaffBadge.badge_id == Badge.id)
                .filter(StaffBadge.emp_id == emp_id)
                .all()
            )
        except Exception:
            emp_badges = []

        # Multitask entries for this staff member (for admin view)
        try:
            from sqlalchemy import desc as _desc
            multitask_entries = MultitaskEntry.query.filter_by(emp_id=emp_id).order_by(MultitaskEntry.entry_date.desc()).limit(30).all()
        except Exception:
            multitask_entries = []

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
            is_admin=bool(session.get("is_admin")),
            mom_delta=mom_delta,
            prev_month_score=prev_month_score,
            monthly_scores=monthly_scores,
            emp_badges=emp_badges,
            multitask_entries=multitask_entries,
        )
    except Exception as e:
        import traceback
        logger.error(f"Staff detail error for emp_id={emp_id}: {e}\n{traceback.format_exc()}")
        flash(f"Error loading staff profile: {str(e)[:120]}", "danger")
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

        _role_names = {"picker": "Operations Picker", "checker": "Operations Checker",
                       "purchaser": "Operations Purchaser", "delivery": "Delivery Staff",
                       "biller": "Billing Staff"}
        emp = Employee(name=name, email=email, staff_type=staff_type,
                       role=_role_names.get(staff_type, f"Operations {staff_type.title()}"))
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
        if staff_type in ("picker", "checker", "purchaser", "delivery", "biller"):
            emp.staff_type = staff_type
            role_names = {"picker": "Operations Picker", "checker": "Operations Checker",
                          "purchaser": "Operations Purchaser", "delivery": "Delivery Staff",
                          "biller": "Billing Staff"}
            emp.role = role_names.get(staff_type, f"Operations {staff_type.title()}")
        # Secondary role (optional; "" = clear)
        sec_raw = request.form.get("secondary_staff_type", None)
        if sec_raw is not None:
            sec = sec_raw.strip()
            if sec == "" or sec == "none":
                emp.secondary_staff_type = None
            elif sec in ("picker", "checker", "purchaser", "delivery", "biller") and sec != emp.staff_type:
                emp.secondary_staff_type = sec
        if new_password:
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
                return redirect(url_for("admin_dashboard"))
            emp.set_password(new_password)

        # Admin Adjustment — ongoing manual +/- points
        adj_raw = request.form.get("admin_adjustment", "").strip()
        if adj_raw != "":
            try:
                adj_val = float(adj_raw)
                # Clamp to sensible range
                adj_val = max(-30.0, min(30.0, adj_val))
                emp.admin_adjustment = adj_val
            except (ValueError, TypeError):
                pass
        adj_note = request.form.get("admin_adjustment_note", "").strip()
        if adj_note:
            emp.admin_adjustment_note = adj_note[:200]

        # Phone number (Feature 15)
        phone_val = request.form.get("phone", "").strip()
        emp.phone = phone_val[:20] if phone_val else None

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
        if not emp:
            flash("User not found.", "danger")
            return redirect(url_for("dashboard"))

        payload = build_pdf_payload(emp)
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

        payload = build_pdf_payload(emp)
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

            # ── VALIDATION: reject submission if required fields missing ──
            missing_fields = []
            if staff_type == "picker":
                if total_bills_received <= 0:
                    missing_fields.append("Total Bills Received (TB Sales)")
                if picked <= 0:
                    missing_fields.append("Items Picked")
            elif staff_type == "checker":
                if bills_received <= 0:
                    missing_fields.append("Bills Received (TB Sales)")
                if checked <= 0:
                    missing_fields.append("Items Checked")
            elif staff_type == "purchaser":
                if sales_bills_open <= 0:
                    missing_fields.append("PO Bills Received (TB Sales)")
                if errors_found <= 0:
                    missing_fields.append("Number of Items")

            if missing_fields:
                flash(f"❌ Cannot save — required fields missing or zero: {', '.join(missing_fields)}.", "danger")
                return redirect(url_for("past_entry", date_str=date_str))

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
                    payload = build_pdf_payload(emp)
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


# ─── CONSTANT PROTOCOL ROUTES (Phase 2) ──────────────────────────────────

@app.route("/validations")
@login_required
def validations_list():
    """Show pending validations waiting for checker input. Visible to all staff."""
    try:
        today = date.today()
        # Show pending validations from last 3 days (so checkers can catch up)
        cutoff = today - timedelta(days=3)
        pending = BillValidation.query.filter(
            BillValidation.status == "pending",
            BillValidation.entry_date >= cutoff
        ).order_by(BillValidation.entry_date.desc(), BillValidation.created_at.desc()).all()
        # Recent completed (last 7 days)
        # Order by created_at (always set) instead of updated_at (may be NULL on old rows)
        recent = BillValidation.query.filter(
            BillValidation.status.in_(["confirmed", "mismatch", "admin_override"]),
            BillValidation.entry_date >= today - timedelta(days=7)
        ).order_by(BillValidation.created_at.desc()).limit(30).all()

        # Build helper maps for display
        emp_map = {e.id: e for e in Employee.query.all()}
        current_user_id = session.get("user_id")

        def _serialize(bv):
            already_voted = current_user_id in [bv.checker1_id, bv.checker2_id, bv.checker3_id]
            return {
                "bv": bv,
                "picker": emp_map.get(bv.picker_id),
                "checker1": emp_map.get(bv.checker1_id) if bv.checker1_id else None,
                "checker2": emp_map.get(bv.checker2_id) if bv.checker2_id else None,
                "checker3": emp_map.get(bv.checker3_id) if bv.checker3_id else None,
                "already_voted": already_voted,
                "slots_filled": bv.checker_count_submitted,
            }

        return render_template("validations.html",
            pending=[_serialize(b) for b in pending],
            recent=[_serialize(b) for b in recent],
            current_user_id=current_user_id,
            is_admin=bool(session.get("is_admin")),
            today=today,
        )
    except Exception as e:
        import traceback
        logger.error(f"validations_list: {e}\n{traceback.format_exc()}")
        flash(f"Error loading validations: {str(e)[:120]}", "danger")
        return redirect(url_for("admin_dashboard") if session.get("is_admin") else url_for("dashboard"))


@app.route("/validation/<int:bv_id>/submit", methods=["POST"])
@login_required
def validation_submit(bv_id):
    """A checker submits their count for a pending validation."""
    try:
        bv = db.session.get(BillValidation, bv_id)
        if not bv:
            flash("Validation not found.", "danger")
            return redirect(url_for("validations_list"))
        if bv.status not in ("pending",):
            flash("This validation is already resolved.", "warning")
            return redirect(url_for("validations_list"))

        current_id = session.get("user_id")
        if current_id == bv.picker_id:
            flash("You cannot validate your own submission.", "warning")
            return redirect(url_for("validations_list"))
        if current_id in [bv.checker1_id, bv.checker2_id, bv.checker3_id]:
            flash("You have already submitted your count for this validation.", "info")
            return redirect(url_for("validations_list"))

        try:
            count = max(0, int(request.form.get("count", 0) or 0))
        except (ValueError, TypeError):
            flash("Invalid count.", "danger")
            return redirect(url_for("validations_list"))

        # Fill the first empty slot
        now = datetime.utcnow()
        if bv.checker1_count is None:
            bv.checker1_id = current_id
            bv.checker1_count = count
            bv.checker1_at = now
        elif bv.checker2_count is None:
            bv.checker2_id = current_id
            bv.checker2_count = count
            bv.checker2_at = now
        elif bv.checker3_count is None:
            bv.checker3_id = current_id
            bv.checker3_count = count
            bv.checker3_at = now
        else:
            flash("All 3 checker slots already filled.", "warning")
            return redirect(url_for("validations_list"))

        # If all 3 slots now filled, evaluate
        if bv.checker_count_submitted >= 3:
            new_status, wrong_ids = bv.evaluate()
            bv.status = new_status
            bv.mismatch_emp_ids = ",".join(str(x) for x in wrong_ids)
            db.session.commit()
            if new_status == "confirmed":
                flash("✅ All 4 counts matched — submission confirmed.", "success")
            else:
                # ── Auto-create complaint entries for each flagged staff ──
                # Deduction scales with how far off they were from the majority.
                try:
                    all_votes = [
                        (bv.picker_id, bv.picker_count),
                        (bv.checker1_id, bv.checker1_count),
                        (bv.checker2_id, bv.checker2_count),
                        (bv.checker3_id, bv.checker3_count),
                    ]
                    counts = Counter(v for _, v in all_votes if v is not None)
                    majority_val, _ = counts.most_common(1)[0]

                    for wid in wrong_ids:
                        wrong_emp = db.session.get(Employee, wid)
                        if not wrong_emp:
                            continue
                        wrong_val = next((v for eid, v in all_votes if eid == wid), 0)
                        diff = abs((wrong_val or 0) - majority_val)
                        # Scale: 0.5 pt per unit difference, capped at 15 pts
                        deduct_pts = min(round(diff * 0.5, 1), 15.0)
                        if deduct_pts < 0.5:
                            deduct_pts = 0.5  # minimum deduction per mismatch

                        # Role-specific deduction routing
                        picker_f = deduct_pts if wrong_emp.staff_type == "picker" else 0.0
                        checker_f = deduct_pts if wrong_emp.staff_type == "checker" else 0.0
                        purchaser_f = deduct_pts if wrong_emp.staff_type == "purchaser" else 0.0

                        auto_complaint = Complaint(
                            entry_date=date.today(),
                            reported_by=None,  # auto-generated, no admin
                            target_emp_id=wid,  # Phase 1 targeted deduction
                            description=(f"[AUTO] Bill count mismatch on {bv.entry_date}. "
                                         f"Entered {wrong_val}, majority was {majority_val} (off by {diff})."),
                            complaint_type="missing",
                            picker_deduct=picker_f,
                            checker_deduct=checker_f,
                            purchaser_deduct=purchaser_f,
                            picker_final=picker_f,
                            checker_final=checker_f,
                            purchaser_final=purchaser_f,
                            is_resolved=False,
                        )
                        db.session.add(auto_complaint)
                    db.session.commit()
                    log_audit("auto_deduction",
                              f"bv_id={bv.id} date={bv.entry_date}",
                              f"{len(wrong_ids)} staff auto-deducted for mismatch")
                except Exception as ae:
                    logger.error(f"Auto-deduction on mismatch failed: {ae}")
                    db.session.rollback()

                # Broadcast mismatch alert to admin
                try:
                    wrong_names = [db.session.get(Employee, wid).name
                                   for wid in wrong_ids if db.session.get(Employee, wid)]
                    socketio.emit("validation_mismatch", {
                        "picker_id": bv.picker_id,
                        "date": str(bv.entry_date),
                        "wrong_names": wrong_names,
                    })
                    # Feature 11: SMS alert for mismatch
                    try:
                        picker_emp = db.session.get(Employee, bv.picker_id)
                        picker_name = picker_emp.name if picker_emp else f"#{bv.picker_id}"
                        admin_phone = os.environ.get("ADMIN_PHONE", "")
                        send_sms(admin_phone,
                                 f"Bill count mismatch: {picker_name} on {bv.entry_date}. {len(wrong_ids)} staff flagged.")
                    except Exception:
                        pass
                except Exception:
                    pass
                flash(f"⚠️ Counts do not match! {len(wrong_ids)} staff flagged and auto-deducted. Admin can review and resolve via Complaints.", "warning")
        else:
            db.session.commit()
            flash(f"✓ Count recorded ({bv.checker_count_submitted}/3 checkers).", "success")

        # Live update
        socketio.emit("validation_update", {
            "bv_id": bv.id,
            "status": bv.status,
            "slots_filled": bv.checker_count_submitted,
        })

        return redirect(url_for("validations_list"))
    except Exception as e:
        db.session.rollback()
        logger.error(f"validation_submit: {e}")
        flash("Error submitting validation.", "danger")
        return redirect(url_for("validations_list"))


@app.route("/admin/validation/<int:bv_id>/override", methods=["POST"])
@admin_required
def admin_validation_override(bv_id):
    """Admin override — accept a pending or mismatched validation.
    Used when fewer than 3 checkers are on shift, or for dispute resolution.
    """
    try:
        bv = db.session.get(BillValidation, bv_id)
        if not bv:
            flash("Validation not found.", "danger")
            return redirect(url_for("validations_list"))

        action = request.form.get("action", "accept")
        note = request.form.get("note", "").strip()[:200]
        admin_id = session.get("user_id")

        if action == "accept":
            bv.status = "admin_override"
            bv.override_by = admin_id
            bv.override_note = note or "Admin override (short-staffed / accepted)"
            db.session.commit()
            log_audit("validation_override", f"picker={bv.picker_id} date={bv.entry_date}",
                      f"Accepted. Note: {note}")
            flash(f"✅ Validation #{bv.id} accepted by admin override.", "success")
        elif action == "reject":
            bv.status = "mismatch"
            bv.override_by = admin_id
            bv.override_note = note or "Admin rejected"
            db.session.commit()
            log_audit("validation_reject", f"picker={bv.picker_id} date={bv.entry_date}",
                      f"Rejected. Note: {note}")
            flash(f"⚠️ Validation #{bv.id} marked as mismatch.", "warning")
        else:
            flash("Unknown action.", "warning")

        return redirect(url_for("validations_list"))
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_validation_override: {e}")
        flash("Error processing override.", "danger")
        return redirect(url_for("validations_list"))


@app.route("/api/last_entry")
@login_required
def api_last_entry():
    """Return the last entry timestamp for every employee (for live Last Entry card)."""
    try:
        # Single query: max entry_date per employee
        latest_subq = (
            db.session.query(
                KPIEntry.emp_id,
                db.func.max(KPIEntry.entry_date).label("last_date")
            ).group_by(KPIEntry.emp_id).subquery()
        )
        employees = Employee.query.filter_by(is_admin=False).all()
        latest_map = {row.emp_id: row.last_date for row in db.session.query(latest_subq).all()}
        out = {}
        for emp in employees:
            out[emp.id] = {
                "name": emp.name,
                "staff_type": emp.staff_type,
                "last_entry_date": str(latest_map[emp.id]) if emp.id in latest_map else None,
            }
        return jsonify(employees=out, server_time=datetime.utcnow().isoformat())
    except Exception as e:
        logger.error(f"api_last_entry: {e}")
        return jsonify(error="Server error"), 500



@app.route("/admin/accept_target_suggestion/<int:emp_id>", methods=["POST"])
@admin_required
def admin_accept_target_suggestion(emp_id):
    """Admin accepts the auto-suggest: raise this employee's hourly target by 40%."""
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        current = emp.custom_hourly_target or (
            50.0 if emp.staff_type == "picker"
            else 70.0 if emp.staff_type == "checker"
            else 30.0
        )
        new_target = round(current * 1.4, 1)
        emp.custom_hourly_target = new_target
        db.session.commit()
        log_audit("raise_target", emp.name,
                  f"Hourly target raised from {current} to {new_target}/hr")
        flash(f"✅ {emp.name}'s hourly target raised to {new_target}/hr (was {current}).", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"accept_target_suggestion: {e}")
        flash("Error updating target.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_target/<int:emp_id>", methods=["POST"])
@admin_required
def admin_reset_target(emp_id):
    """Reset employee's custom target back to role default."""
    try:
        emp = db.session.get(Employee, emp_id)
        if emp:
            emp.custom_hourly_target = None
            db.session.commit()
            log_audit("reset_target", emp.name, "Reset to role default")
            flash(f"✅ {emp.name}'s target reset to role default.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"reset_target: {e}")
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

# ─── FEATURE 2: ANNOUNCEMENTS ────────────────────────────────────────────────

@app.route("/admin/announcement/create", methods=["POST"])
@admin_required
def admin_create_announcement():
    try:
        title = request.form.get("title", "").strip()[:150]
        body = request.form.get("body", "").strip()
        expires_str = request.form.get("expires_at", "").strip()
        if not title or not body:
            flash("Title and body are required.", "warning")
            return redirect(url_for("admin_dashboard"))
        expires_at = None
        if expires_str:
            try:
                expires_at = datetime.strptime(expires_str, "%Y-%m-%d")
            except ValueError:
                pass
        ann = Announcement(
            title=title, body=body,
            created_by=session.get("user_id"),
            is_active=True, expires_at=expires_at
        )
        db.session.add(ann)
        db.session.commit()
        flash(f"Announcement '{title}' created.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"create_announcement: {e}")
        flash("Error creating announcement.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/announcement/<int:ann_id>/deactivate", methods=["POST"])
@admin_required
def admin_deactivate_announcement(ann_id):
    try:
        ann = db.session.get(Announcement, ann_id)
        if ann:
            ann.is_active = False
            db.session.commit()
            flash("Announcement deactivated.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"deactivate_announcement: {e}")
        flash("Error.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/announcement/<int:ann_id>/dismiss", methods=["POST"])
@login_required
def dismiss_announcement(ann_id):
    try:
        emp_id = session.get("user_id")
        existing = AnnouncementRead.query.filter_by(
            announcement_id=ann_id, emp_id=emp_id
        ).first()
        if not existing:
            ar = AnnouncementRead(announcement_id=ann_id, emp_id=emp_id)
            db.session.add(ar)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"dismiss_announcement: {e}")
    return redirect(url_for("dashboard"))


# ─── FEATURE 3: PERSONAL GOAL SETTING ────────────────────────────────────────

@app.route("/goal/set", methods=["POST"])
@login_required
def goal_set():
    try:
        emp_id = session.get("user_id")
        target = float(request.form.get("target_score", 70) or 70)
        target = max(10.0, min(100.0, target))
        month_str = date.today().strftime("%Y-%m")
        existing = StaffGoal.query.filter_by(emp_id=emp_id, month=month_str).first()
        if existing:
            existing.target_score = target
            existing.updated_at = datetime.utcnow()
        else:
            goal = StaffGoal(emp_id=emp_id, target_score=target, month=month_str)
            db.session.add(goal)
        db.session.commit()
        flash(f"Goal set to {target} pts for {month_str}.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"goal_set: {e}")
        flash("Error setting goal.", "danger")
    return redirect(url_for("dashboard"))


# ─── FEATURE 4: PAST ENTRY REQUESTS ──────────────────────────────────────────

@app.route("/past_entry_request", methods=["POST"])
@login_required
def past_entry_request_submit():
    try:
        emp_id = session.get("user_id")
        date_str = request.form.get("requested_date", "").strip()
        reason = request.form.get("reason", "").strip()[:200]
        if not date_str or not reason:
            flash("Date and reason required.", "warning")
            return redirect(url_for("dashboard"))
        req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = date.today()
        if req_date >= today:
            flash("Can only request past dates.", "warning")
            return redirect(url_for("dashboard"))
        existing_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=req_date).first()
        if existing_entry:
            flash("You already have an entry for that date.", "info")
            return redirect(url_for("dashboard"))
        existing_req = PastEntryRequest.query.filter_by(
            emp_id=emp_id, requested_date=req_date, status="pending"
        ).first()
        if existing_req:
            flash("You already have a pending request for that date.", "info")
            return redirect(url_for("dashboard"))
        req = PastEntryRequest(
            emp_id=emp_id, requested_date=req_date, reason=reason, status="pending"
        )
        db.session.add(req)
        db.session.commit()
        flash("Past entry request submitted. Admin will review.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"past_entry_request: {e}")
        flash("Error submitting request.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/admin/past_request/<int:req_id>/approve", methods=["POST"])
@admin_required
def admin_approve_past_request(req_id):
    try:
        req = db.session.get(PastEntryRequest, req_id)
        if not req:
            flash("Request not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        req.status = "approved"
        req.resolved_at = datetime.utcnow()
        window = PastEntryWindow.query.filter_by(past_date=req.requested_date).first()
        if window:
            window.is_active = True
            window.opened_by = session.get("user_id")
            window.opened_at = datetime.utcnow()
        else:
            window = PastEntryWindow(
                past_date=req.requested_date,
                opened_by=session.get("user_id"),
                is_active=True
            )
            db.session.add(window)
        db.session.commit()
        flash(f"Request approved. Entry window opened for {req.requested_date.strftime('%d %b %Y')}.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"approve_past_request: {e}")
        flash("Error approving request.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/past_request/<int:req_id>/reject", methods=["POST"])
@admin_required
def admin_reject_past_request(req_id):
    try:
        req = db.session.get(PastEntryRequest, req_id)
        if not req:
            flash("Request not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        req.status = "rejected"
        req.admin_note = request.form.get("admin_note", "").strip()[:200]
        req.resolved_at = datetime.utcnow()
        db.session.commit()
        flash("Request rejected.", "warning")
    except Exception as e:
        db.session.rollback()
        logger.error(f"reject_past_request: {e}")
        flash("Error rejecting request.", "danger")
    return redirect(url_for("admin_dashboard"))


# ─── FEATURE 5: BADGE SYSTEM ──────────────────────────────────────────────────

def compute_auto_badges(emp_id):
    """Compute and award auto badges for an employee after a KPI submission."""
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            return
        entries = KPIEntry.query.filter_by(emp_id=emp_id).order_by(KPIEntry.entry_date.desc()).all()
        if not entries:
            return
        badges = Badge.query.filter_by(badge_type="auto").all()
        today = date.today()
        month_str = today.strftime("%Y-%m")
        for badge in badges:
            existing = StaffBadge.query.filter_by(emp_id=emp_id, badge_id=badge.id).first()
            if existing:
                continue
            earned = False
            try:
                if badge.name == "Perfect Week":
                    sorted_entries = sorted(entries, key=lambda e: e.entry_date, reverse=True)
                    consec = 0
                    for e in sorted_entries:
                        t = (e.picked or 0) + (e.missed or 0)
                        acc = round((e.picked or 0) / t * 100, 1) if t > 0 else 0
                        if acc >= 100:
                            consec += 1
                        else:
                            break
                    earned = consec >= 5
                elif badge.name == "Speed Demon":
                    month_entries = [e for e in entries if e.entry_date.strftime("%Y-%m") == month_str]
                    if month_entries:
                        total_items = sum((e.picked or 0) + (e.missed or 0) for e in month_entries)
                        total_hrs = 9.0 * len(month_entries)
                        avg_speed = total_items / total_hrs if total_hrs > 0 else 0
                        earned = avg_speed > 60
                elif badge.name == "Clean Sweep":
                    week_entries = [e for e in entries if (today - e.entry_date).days <= 6]
                    if len(week_entries) >= 5:
                        earned = all(
                            (e.rack_organized or 0) >= 10 and (e.table_clean or 0) == 1 and (e.sweep_done or 0) == 1
                            for e in week_entries
                        )
                elif badge.name == "Century Club":
                    earned = any((e.picked or 0) >= 100 for e in entries)
                elif badge.name == "Top Performer":
                    month_entries_emp = [e for e in entries if e.entry_date.strftime("%Y-%m") == month_str]
                    if month_entries_emp:
                        my_stats = build_analytics(month_entries_emp, emp.staff_type, emp_id=emp_id)
                        if my_stats:
                            all_staff_role = Employee.query.filter_by(is_admin=False, staff_type=emp.staff_type).all()
                            top = True
                            for s in all_staff_role:
                                if s.id == emp_id:
                                    continue
                                s_ents = [e for e in KPIEntry.query.filter_by(emp_id=s.id).all()
                                          if e.entry_date.strftime("%Y-%m") == month_str]
                                s_stats = build_analytics(s_ents, s.staff_type, emp_id=s.id)
                                if s_stats and s_stats["eff_score"] > my_stats["eff_score"]:
                                    top = False
                                    break
                            earned = top
                elif badge.name == "7-Day Streak":
                    dates_set = set(str(e.entry_date) for e in entries)
                    streak = 0
                    for i in range(30):
                        d = today - timedelta(days=i)
                        if str(d) in dates_set:
                            streak += 1
                        else:
                            break
                    earned = streak >= 7
            except Exception as be:
                logger.warning(f"Badge check {badge.name}: {be}")
                continue
            if earned:
                try:
                    sb = StaffBadge(emp_id=emp_id, badge_id=badge.id, awarded_by=None, note="Auto-awarded")
                    db.session.add(sb)
                    db.session.commit()
                    logger.info(f"Badge '{badge.name}' awarded to emp_id={emp_id}")
                except Exception as dbe:
                    db.session.rollback()
                    logger.warning(f"Badge award failed: {dbe}")
    except Exception as e:
        logger.error(f"compute_auto_badges: {e}")


def compute_delivery_badges(emp_id):
    """Award delivery-related auto badges based on completed DeliveryTrip records."""
    try:
        completed_trips = DeliveryTrip.query.filter_by(emp_id=emp_id, status="completed").all()
        total = len(completed_trips)
        on_time_count = sum(1 for t in completed_trips if t.is_on_time)

        badge_checks = [
            ("First Delivery",     total >= 1),
            ("Speed Rider",        on_time_count >= 3),
            ("Veteran Courier",    total >= 50),
        ]
        # Perfect Courier: check for 10 consecutive on-time
        consecutive = 0
        max_consec = 0
        for t in sorted(completed_trips, key=lambda x: x.created_at or datetime.min):
            if t.is_on_time:
                consecutive += 1
                max_consec = max(max_consec, consecutive)
            else:
                consecutive = 0
        badge_checks.append(("Perfect Courier", max_consec >= 10))

        for badge_name, earned in badge_checks:
            if not earned:
                continue
            badge = Badge.query.filter_by(name=badge_name).first()
            if not badge:
                continue
            existing = StaffBadge.query.filter_by(emp_id=emp_id, badge_id=badge.id).first()
            if not existing:
                db.session.add(StaffBadge(emp_id=emp_id, badge_id=badge.id, note="auto-delivery"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"compute_delivery_badges: {e}")


def score_delivery_trip(trip):
    """Return point score for a completed delivery trip."""
    score = 10  # base completion
    if trip.is_on_time:
        score += 20  # on-time bonus
    if trip.duration_minutes and trip.duration_minutes < 30:
        score += 5   # speed bonus
    return score


@app.route("/admin/badge/award", methods=["POST"])
@admin_required
def admin_award_badge():
    try:
        emp_id = int(request.form.get("emp_id", 0))
        badge_id = int(request.form.get("badge_id", 0))
        note = request.form.get("note", "").strip()[:200]
        if not emp_id or not badge_id:
            flash("Employee and badge required.", "warning")
            return redirect(url_for("admin_dashboard"))
        existing = StaffBadge.query.filter_by(emp_id=emp_id, badge_id=badge_id).first()
        if existing:
            flash("Employee already has this badge.", "info")
            return redirect(url_for("admin_dashboard"))
        sb = StaffBadge(
            emp_id=emp_id, badge_id=badge_id,
            awarded_by=session.get("user_id"), note=note
        )
        db.session.add(sb)
        db.session.commit()
        flash("Badge awarded.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_award_badge: {e}")
        flash("Error awarding badge.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/badge/create", methods=["POST"])
@admin_required
def admin_create_badge():
    try:
        name = request.form.get("name", "").strip()[:100]
        description = request.form.get("description", "").strip()[:200]
        icon = request.form.get("icon", "🏅").strip()[:10]
        if not name or not description:
            flash("Name and description required.", "warning")
            return redirect(url_for("admin_dashboard"))
        if Badge.query.filter_by(name=name).first():
            flash(f"Badge '{name}' already exists.", "warning")
            return redirect(url_for("admin_dashboard"))
        badge = Badge(name=name, description=description, icon=icon, badge_type="manual")
        db.session.add(badge)
        db.session.commit()
        flash(f"Badge '{name}' created.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_create_badge: {e}")
        flash("Error creating badge.", "danger")
    return redirect(url_for("admin_dashboard"))


# ─── FEATURE 6: BULK CSV IMPORT ──────────────────────────────────────────────

@app.route("/admin/bulk_import", methods=["GET", "POST"])
@admin_required
def admin_bulk_import():
    if request.method == "GET":
        return render_template("bulk_import.html")
    try:
        f = request.files.get("csv_file")
        if not f or not f.filename:
            flash("Please upload a CSV file.", "warning")
            return render_template("bulk_import.html")
        content = f.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        imported = 0
        skipped = []
        for row in reader:
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            password = (row.get("password") or "").strip()
            staff_type = (row.get("staff_type") or "picker").strip().lower()
            if not name or not email or not password:
                skipped.append(f"{email or name}: missing fields")
                continue
            if len(password) < 6:
                skipped.append(f"{email}: password too short")
                continue
            if staff_type not in ("picker", "checker", "purchaser"):
                skipped.append(f"{email}: invalid staff_type '{staff_type}'")
                continue
            if Employee.query.filter_by(email=email).first():
                skipped.append(f"{email}: email already exists")
                continue
            emp = Employee(
                name=name, email=email,
                staff_type=staff_type,
                role=f"Operations {staff_type.title()}"
            )
            emp.set_password(password)
            db.session.add(emp)
            imported += 1
        db.session.commit()
        msg = f"Imported {imported} employee(s)."
        if skipped:
            msg += f" Skipped {len(skipped)}: " + "; ".join(skipped[:5])
            if len(skipped) > 5:
                msg += f" ... and {len(skipped)-5} more."
        flash(msg, "success" if imported > 0 else "warning")
    except Exception as e:
        db.session.rollback()
        logger.error(f"bulk_import: {e}")
        flash(f"Error processing CSV: {str(e)[:200]}", "danger")
    return render_template("bulk_import.html")


# ─── FEATURE 7: CUSTOM DATE RANGE ANALYTICS API ──────────────────────────────

@app.route("/api/analytics/range")
@login_required
def api_analytics_range():
    try:
        emp_id = request.args.get("emp_id", type=int)
        start_str = request.args.get("start", "")
        end_str = request.args.get("end", "")
        if not emp_id:
            return jsonify(error="emp_id required"), 400
        if not session.get("is_admin") and session.get("user_id") != emp_id:
            return jsonify(error="Unauthorized"), 403
        emp = db.session.get(Employee, emp_id)
        if not emp:
            return jsonify(error="Employee not found"), 404
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify(error="Invalid date format. Use YYYY-MM-DD"), 400
        entries = KPIEntry.query.filter(
            KPIEntry.emp_id == emp_id,
            KPIEntry.entry_date >= start_date,
            KPIEntry.entry_date <= end_date
        ).order_by(KPIEntry.entry_date.desc()).all()
        stats = build_analytics(entries, emp.staff_type, emp_id=emp_id)
        if not stats:
            return jsonify(stats=None, days=0, message="No entries in this range")
        return jsonify(stats=stats, days=len(entries), emp_name=emp.name, staff_type=emp.staff_type)
    except Exception as e:
        logger.error(f"api_analytics_range: {e}")
        return jsonify(error="Server error"), 500


# ─── FEATURE 11: SMS/WHATSAPP NOTIFICATIONS (TWILIO) ─────────────────────────
# Note: send_sms is defined earlier in the file (around line 1008); this is a stub
# reference comment only — no duplicate definition.


# ─── FEATURE 12: MONTHLY PDF EMAIL (APScheduler) ─────────────────────────────

def send_monthly_reports():
    """Send monthly KPI PDF reports to admin email."""
    with app.app_context():
        admin_email = os.environ.get("ADMIN_EMAIL_RECIPIENT", "")
        if not admin_email or not os.environ.get("MAIL_SERVER"):
            logger.warning("Mail env vars not set — skipping monthly reports.")
            return
        try:
            from utils import generate_visual_pdf
            employees = Employee.query.filter_by(is_admin=False).all()
            now = datetime.utcnow()
            month_label = now.strftime("%B %Y")
            msg = MailMessage(
                subject=f"Monthly KPI Report — {month_label}",
                recipients=[admin_email],
                body=f"Please find attached the monthly KPI reports for {month_label}.",
                sender=app.config.get("MAIL_DEFAULT_SENDER", "noreply@pharmaip.com")
            )
            for emp in employees:
                try:
                    payload = build_pdf_payload(emp)
                    pdf_buf = generate_visual_pdf(emp.name, payload)
                    safe_name = emp.name.replace(" ", "_").replace("/", "-")
                    msg.attach(
                        f"KRA_{safe_name}_{now.strftime('%Y-%m')}.pdf",
                        "application/pdf",
                        pdf_buf.getvalue()
                    )
                except Exception as pe:
                    logger.error(f"PDF for {emp.name}: {pe}")
            mail.send(msg)
            logger.info(f"Monthly report email sent to {admin_email}")
        except Exception as e:
            logger.error(f"send_monthly_reports: {e}")


@app.route("/admin/send_monthly_report")
@admin_required
def admin_send_monthly_report():
    """Manual trigger for monthly PDF email."""
    try:
        send_monthly_reports()
        flash("Monthly reports sent (check server logs if mail not configured).", "success")
    except Exception as e:
        logger.error(f"admin_send_monthly_report: {e}")
        flash("Error sending reports.", "danger")
    return redirect(url_for("admin_dashboard"))


# ─── FEATURE 14: Admin Staff Notes ───────────────────────────────────────────

@app.route("/admin/staff_note/<int:emp_id>", methods=["POST"])
@admin_required
def admin_staff_note(emp_id):
    """Admin adds a timestamped note to an employee record."""
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        note_text = request.form.get("note", "").strip()
        if not note_text:
            flash("Note cannot be empty.", "warning")
            return redirect(url_for("admin_dashboard"))
        if len(note_text) > 500:
            note_text = note_text[:500]
        note = AdminStaffNote(
            emp_id=emp_id,
            note=note_text,
            created_by=session.get("user_id"),
        )
        db.session.add(note)
        db.session.commit()
        log_audit("staff_note_added", emp.name, f"Note: {note_text[:60]}")
        flash(f"✅ Note added for {emp.name}.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_staff_note: {e}")
        flash("Error saving note.", "danger")
    return redirect(url_for("admin_dashboard"))


# ─── FEATURE 15: Password Reset via Phone OTP ────────────────────────────────

@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    """Step 1: Enter phone number to receive OTP."""
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            flash("Please enter your phone number.", "warning")
            return render_template("forgot_password.html")
        emp = Employee.query.filter_by(phone=phone).first()
        if not emp:
            flash("If that number is registered, an OTP has been sent.", "info")
            return render_template("forgot_password.html")
        otp_code = "".join(random.choices(string.digits, k=6))
        expires_at = datetime.utcnow() + timedelta(minutes=30)
        try:
            PasswordResetOTP.query.filter_by(emp_id=emp.id, is_used=False).update({"is_used": True})
            db.session.commit()
        except Exception:
            db.session.rollback()
        otp_obj = PasswordResetOTP(
            emp_id=emp.id,
            otp_code=otp_code,
            expires_at=expires_at,
        )
        db.session.add(otp_obj)
        db.session.commit()
        sms_sent = send_sms(phone, f"Your Pharma IP password reset OTP is: {otp_code}. Valid for 30 minutes. Do not share.")
        if not sms_sent:
            logger.warning(f"OTP SMS not sent (Twilio not configured). OTP for emp {emp.id}: {otp_code}")
        flash("OTP sent to your registered number.", "info")
        return redirect(url_for("reset_password", phone=phone))
    return render_template("forgot_password.html")


@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    """Step 2: Enter OTP + new password."""
    phone = request.args.get("phone", "").strip() or request.form.get("phone", "").strip()
    if request.method == "POST":
        otp_input = request.form.get("otp", "").strip()
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        if not otp_input or not new_pw:
            flash("Please fill in all fields.", "warning")
            return render_template("reset_password.html", phone=phone)
        if new_pw != confirm_pw:
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", phone=phone)
        if len(new_pw) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("reset_password.html", phone=phone)
        emp = Employee.query.filter_by(phone=phone).first()
        if not emp:
            flash("Invalid or expired reset link.", "danger")
            return redirect(url_for("forgot_password"))
        otp_obj = PasswordResetOTP.query.filter_by(
            emp_id=emp.id, otp_code=otp_input, is_used=False
        ).filter(PasswordResetOTP.expires_at >= datetime.utcnow()).first()
        if not otp_obj:
            flash("OTP is invalid or has expired. Please request a new one.", "danger")
            return render_template("reset_password.html", phone=phone)
        emp.set_password(new_pw)
        otp_obj.is_used = True
        db.session.commit()
        log_audit("password_reset_otp", emp.name, "Password reset via phone OTP")
        flash("✅ Password reset successfully. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", phone=phone)


# ─── FEATURE 16: Monthly PDF Archive ─────────────────────────────────────────

def generate_monthly_report_job(month_str=None):
    """Generate PDF ZIP for all staff and store metadata in MonthlyReportArchive."""
    with app.app_context():
        try:
            import zipfile
            from utils import generate_visual_pdf

            if month_str is None:
                today = date.today()
                first_of_this_month = today.replace(day=1)
                prev_month = first_of_this_month - timedelta(days=1)
                month_str = prev_month.strftime("%Y-%m")

            employees = Employee.query.filter_by(is_admin=False).all()
            if not employees:
                logger.info(f"Monthly report {month_str}: no staff found")
                return

            zip_buf = io.BytesIO()
            pdf_count = 0
            with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for emp in employees:
                    try:
                        payload = build_pdf_payload(emp)
                        pdf_buf = generate_visual_pdf(emp.name, payload)
                        safe_name = emp.name.replace(" ", "_").replace("/", "-")
                        zf.writestr(f"KRA_{safe_name}_{month_str}.pdf", pdf_buf.getvalue())
                        pdf_count += 1
                    except Exception as e:
                        logger.error(f"Monthly report PDF for {emp.name}: {e}")
            zip_buf.seek(0)

            reports_dir = os.path.join(os.path.dirname(__file__), "reports")
            os.makedirs(reports_dir, exist_ok=True)
            file_path = os.path.join(reports_dir, f"monthly_{month_str}.zip")
            with open(file_path, "wb") as f:
                f.write(zip_buf.getvalue())

            existing = MonthlyReportArchive.query.filter_by(month_str=month_str).first()
            if existing:
                existing.generated_at = datetime.utcnow()
                existing.emp_count = pdf_count
                existing.file_path = file_path
            else:
                archive = MonthlyReportArchive(
                    month_str=month_str,
                    generated_at=datetime.utcnow(),
                    emp_count=pdf_count,
                    file_path=file_path,
                )
                db.session.add(archive)
            db.session.commit()
            logger.info(f"✅ Monthly report {month_str} generated: {pdf_count} PDFs")
        except Exception as e:
            logger.error(f"generate_monthly_report_job: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass


@app.route("/admin/reports")
@admin_required
def admin_reports_archive():
    """List all archived monthly reports."""
    try:
        archives = MonthlyReportArchive.query.order_by(MonthlyReportArchive.month_str.desc()).all()
        return render_template("admin_reports.html", archives=archives)
    except Exception as e:
        logger.error(f"admin_reports_archive: {e}")
        flash("Error loading reports archive.", "danger")
        return redirect(url_for("admin_dashboard"))


@app.route("/admin/reports/<month_str>")
@admin_required
def admin_reports_download(month_str):
    """Regenerate and serve the ZIP for a specific month."""
    try:
        import zipfile
        from utils import generate_visual_pdf

        datetime.strptime(month_str, "%Y-%m")

        employees = Employee.query.filter_by(is_admin=False).all()
        if not employees:
            flash("No staff found.", "warning")
            return redirect(url_for("admin_reports_archive"))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for emp in employees:
                try:
                    payload = build_pdf_payload(emp)
                    pdf_buf = generate_visual_pdf(emp.name, payload)
                    safe_name = emp.name.replace(" ", "_").replace("/", "-")
                    zf.writestr(f"KRA_{safe_name}_{month_str}.pdf", pdf_buf.getvalue())
                except Exception as e:
                    logger.error(f"Monthly report PDF for {emp.name}: {e}")

        existing = MonthlyReportArchive.query.filter_by(month_str=month_str).first()
        if existing:
            existing.generated_at = datetime.utcnow()
            db.session.commit()

        zip_buf.seek(0)
        log_audit("monthly_report_download", month_str, "Admin downloaded monthly report")
        return Response(
            zip_buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=AllStaff_KRA_{month_str}.zip"}
        )
    except ValueError:
        flash("Invalid month format.", "danger")
        return redirect(url_for("admin_reports_archive"))
    except Exception as e:
        logger.error(f"admin_reports_download: {e}")
        flash("Error generating report.", "danger")
        return redirect(url_for("admin_reports_archive"))


# ─── FEATURE 17: Stale Validations SMS job ────────────────────────────────────

def stale_validations_sms_job():
    """Daily APScheduler job: SMS admin if stale pending validations exist."""
    with app.app_context():
        try:
            cutoff = datetime.utcnow() - timedelta(hours=24)
            count = BillValidation.query.filter(
                BillValidation.status == 'pending',
                BillValidation.created_at <= cutoff
            ).count()
            if count > 0:
                admin_phone = os.environ.get("ADMIN_PHONE", "")
                if admin_phone:
                    send_sms(admin_phone, f"⚠️ {count} bill validations have been pending for 24+ hours. Please review.")
                    logger.info(f"Stale validations SMS sent: {count} pending")
                else:
                    logger.info(f"Stale validations: {count} pending, ADMIN_PHONE not set")
        except Exception as e:
            logger.error(f"stale_validations_sms_job: {e}")


# ─── DELIVERY TRACKING MODULE ─────────────────────────────────────────────────

STORE_LAT = float(os.environ.get("STORE_LAT", "0.0"))
STORE_LNG = float(os.environ.get("STORE_LNG", "0.0"))
STORE_RADIUS_M = 400    # meters — must be within this to start a trip
ARRIVAL_RADIUS_M = 200  # meters — must be within this to confirm delivery
TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "")  # free key → live-traffic routing


def _haversine_m(lat1, lng1, lat2, lng2):
    """Distance in metres between two lat/lng points."""
    import math
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def geocode_address(address):
    """Convert a text address to (lat, lng) using Nominatim (free, no API key)."""
    try:
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": "IndianPharmaKPI/1.0 (delivery-geocoder)"},
            timeout=6,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        logger.warning(f"geocode_address failed for '{address}': {e}")
    return None, None


@app.route("/delivery/assign", methods=["POST"])
@login_required
def delivery_assign():
    """Purchaser creates a delivery assignment — auto-geocodes the destination address."""
    emp_id = session.get("user_id")
    staff_type = session.get("staff_type", "")
    if staff_type not in ("biller",) and not session.get("is_admin"):
        flash("Only billers can assign deliveries.", "danger")
        return redirect(url_for("dashboard"))
    try:
        delivery_emp_id  = int(request.form.get("delivery_emp_id", 0))
        destination_addr = request.form.get("destination_addr", "").strip()
        package_desc     = request.form.get("package_desc", "").strip()
        bills_count      = int(request.form.get("bills_count", 0) or 0)
        recipient_name   = request.form.get("recipient_name", "").strip()
        company_name     = request.form.get("company_name", "").strip()
        notes            = request.form.get("notes", "").strip()[:500]

        if not delivery_emp_id or not destination_addr or not package_desc:
            flash("Please fill in delivery employee, destination, and package description.", "warning")
            return redirect(url_for("dashboard"))

        delivery_emp = db.session.get(Employee, delivery_emp_id)
        if not delivery_emp or (delivery_emp.staff_type != "delivery" and delivery_emp.secondary_staff_type != "delivery"):
            flash("Selected employee is not a delivery staff member.", "warning")
            return redirect(url_for("dashboard"))

        # Auto-geocode — runs in background; if it fails the assignment still saves
        dest_lat, dest_lng = geocode_address(destination_addr)

        assignment = DeliveryAssignment(
            purchaser_id=emp_id,
            delivery_emp_id=delivery_emp_id,
            destination_addr=destination_addr,
            destination_lat=dest_lat,
            destination_lng=dest_lng,
            package_desc=package_desc,
            bills_count=bills_count,
            recipient_name=recipient_name,
            company_name=company_name,
            notes=notes,
            status="pending",
        )
        db.session.add(assignment)
        db.session.commit()
        log_audit("delivery_assign", delivery_emp.name, f"Package: {package_desc[:50]}")
        geo_note = " (map location set)" if dest_lat else " (map will use address text)"
        flash(f"✅ Delivery assigned to {delivery_emp.name}.{geo_note}", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_assign: {e}")
        flash("Error creating delivery assignment.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/delivery/start_trip/<int:assignment_id>", methods=["POST"])
@login_required
def delivery_start_trip(assignment_id):
    """Delivery employee starts a trip — GPS must confirm they're near the store."""
    emp_id = session.get("user_id")
    try:
        assignment = db.session.get(DeliveryAssignment, assignment_id)
        if not assignment or assignment.delivery_emp_id != emp_id:
            return jsonify({"ok": False, "error": "Assignment not found"}), 404
        if assignment.status != "pending":
            return jsonify({"ok": False, "error": "Assignment not in pending state"}), 400

        data = request.get_json(force=True, silent=True) or {}
        curr_lat = float(data.get("lat", 0))
        curr_lng = float(data.get("lng", 0))

        if STORE_LAT != 0.0 and STORE_LNG != 0.0:
            dist = _haversine_m(curr_lat, curr_lng, STORE_LAT, STORE_LNG)
            if dist > STORE_RADIUS_M:
                return jsonify({"ok": False, "error": f"You must be at the store to start a trip (you are {int(dist)}m away, max {STORE_RADIUS_M}m)."}), 400

        assignment.status = "in_transit"
        trip = DeliveryTrip(
            assignment_id=assignment_id,
            emp_id=emp_id,
            trip_date=date.today(),
            departure_lat=curr_lat,
            departure_lng=curr_lng,
            departure_time=datetime.utcnow(),
            status="active",
        )
        db.session.add(trip)
        db.session.commit()
        return jsonify({"ok": True, "trip_id": trip.id,
                        "dest_lat": assignment.destination_lat,
                        "dest_lng": assignment.destination_lng})
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_start_trip: {e}")
        return jsonify({"ok": False, "error": "Server error"}), 500


@app.route("/delivery/check_arrival/<int:assignment_id>", methods=["POST"])
@login_required
def delivery_check_arrival(assignment_id):
    """Check if delivery employee is within ARRIVAL_RADIUS_M of destination. Returns unlock flag."""
    emp_id = session.get("user_id")
    try:
        assignment = db.session.get(DeliveryAssignment, assignment_id)
        if not assignment or assignment.delivery_emp_id != emp_id:
            return jsonify({"ok": False, "error": "Not found"}), 404

        data = request.get_json(force=True, silent=True) or {}
        curr_lat = float(data.get("lat", 0))
        curr_lng = float(data.get("lng", 0))

        if not assignment.destination_lat or not assignment.destination_lng:
            return jsonify({"ok": True, "unlock": True, "dist": 0})

        # Skip GPS check if device sent null/zero coords (GPS unavailable)
        if curr_lat == 0 and curr_lng == 0:
            return jsonify({"ok": True, "unlock": True, "dist": 0})

        dist = _haversine_m(curr_lat, curr_lng, assignment.destination_lat, assignment.destination_lng)
        unlock = dist <= ARRIVAL_RADIUS_M
        return jsonify({"ok": True, "unlock": unlock, "dist": int(dist), "radius": ARRIVAL_RADIUS_M})
    except Exception as e:
        logger.error(f"delivery_check_arrival: {e}")
        return jsonify({"ok": False, "error": "Server error"}), 500


@app.route("/delivery/confirm/<int:assignment_id>", methods=["POST"])
@login_required
def delivery_confirm(assignment_id):
    """Delivery employee confirms delivery — GPS check, marks assignment delivered."""
    emp_id = session.get("user_id")
    try:
        assignment = db.session.get(DeliveryAssignment, assignment_id)
        if not assignment or assignment.delivery_emp_id != emp_id:
            return jsonify({"ok": False, "error": "Not found"}), 404
        if assignment.status not in ("in_transit", "pending"):
            return jsonify({"ok": False, "error": "Cannot confirm this assignment"}), 400

        data = request.get_json(force=True, silent=True) or {}
        curr_lat      = float(data.get("lat", 0))
        curr_lng      = float(data.get("lng", 0))
        delivered_to  = data.get("delivered_to", "").strip()
        company       = data.get("company", "").strip()
        delivery_note = data.get("note", "").strip()[:500]

        if not delivered_to:
            return jsonify({"ok": False, "error": "Please enter the name of the person who received the package"}), 400

        # Only enforce GPS geofence when the device actually sent a valid position
        if assignment.destination_lat and assignment.destination_lng and not (curr_lat == 0 and curr_lng == 0):
            dist = _haversine_m(curr_lat, curr_lng, assignment.destination_lat, assignment.destination_lng)
            if dist > ARRIVAL_RADIUS_M:
                return jsonify({"ok": False, "error": f"You must be at the destination to confirm delivery ({int(dist)}m away, max {ARRIVAL_RADIUS_M}m)."}), 400

        trip = DeliveryTrip.query.filter_by(assignment_id=assignment_id).first()
        now = datetime.utcnow()
        is_on_time = None

        if trip:
            trip.arrival_time         = now
            trip.delivered_to_name    = delivered_to
            trip.delivered_to_company = company
            trip.delivery_note        = delivery_note
            trip.status               = "completed"
            if trip.departure_time:
                duration = (now - trip.departure_time).total_seconds() / 60.0
                trip.duration_minutes = round(duration, 1)
                is_on_time = duration <= 60.0
                trip.is_on_time = is_on_time
        else:
            trip = DeliveryTrip(
                assignment_id=assignment_id,
                emp_id=emp_id,
                trip_date=date.today(),
                arrival_time=now,
                delivered_to_name=delivered_to,
                delivered_to_company=company,
                delivery_note=delivery_note,
                status="completed",
                is_on_time=None,
            )
            db.session.add(trip)
            is_on_time = None

        assignment.status = "delivered"
        db.session.commit()

        try:
            compute_delivery_badges(emp_id)
        except Exception:
            pass

        log_audit("delivery_confirm", str(emp_id), f"Delivered to {delivered_to} @ {company}")
        return jsonify({"ok": True, "message": "Delivery confirmed!", "on_time": is_on_time})
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_confirm: {e}")
        return jsonify({"ok": False, "error": "Server error"}), 500


@app.route("/delivery/leaderboard")
@login_required
def delivery_leaderboard():
    """Delivery staff leaderboard — ranked by monthly score."""
    try:
        today = date.today()
        month_start = today.replace(day=1)
        delivery_staff = Employee.query.filter(
            Employee.is_admin == False,
            or_(Employee.staff_type == "delivery",
                Employee.secondary_staff_type == "delivery")
        ).all()
        leaderboard = []
        for emp in delivery_staff:
            all_trips   = DeliveryTrip.query.filter_by(emp_id=emp.id, status="completed").all()
            month_trips = [t for t in all_trips if t.trip_date and t.trip_date >= month_start]
            total       = len(all_trips)
            on_time     = sum(1 for t in all_trips if t.is_on_time)
            month_total = len(month_trips)
            month_score = sum(score_delivery_trip(t) for t in month_trips)
            all_durs    = [t.duration_minutes for t in all_trips if t.duration_minutes]
            avg_dur     = round(sum(all_durs) / len(all_durs), 1) if all_durs else 0.0
            leaderboard.append({
                "emp": emp,
                "total": total,
                "on_time": on_time,
                "on_time_pct": round(on_time / max(total, 1) * 100, 1),
                "month_total": month_total,
                "month_score": month_score,
                "avg_dur": avg_dur,
            })
        leaderboard.sort(key=lambda x: x["month_score"], reverse=True)
        return render_template("delivery_leaderboard.html",
                               leaderboard=leaderboard,
                               current_month=today.strftime("%B %Y"),
                               today=today)
    except Exception as e:
        logger.error(f"delivery_leaderboard: {e}")
        flash("Error loading leaderboard.", "danger")
        return redirect(url_for("dashboard"))


@app.route("/admin/delivery")
@admin_required
def admin_delivery():
    """Admin panel: all delivery assignments and trips with analysis."""
    try:
        today       = date.today()
        month_start = today.replace(day=1)
        cutoff_30   = today - timedelta(days=30)

        assignments = (
            DeliveryAssignment.query
            .order_by(DeliveryAssignment.assigned_at.desc())
            .limit(200).all()
        )
        all_trips = DeliveryTrip.query.order_by(DeliveryTrip.trip_date.desc()).all()
        trip_map  = {t.assignment_id: t for t in all_trips}

        all_emp        = Employee.query.all()
        emp_map        = {e.id: e for e in all_emp}
        delivery_staff = [e for e in all_emp if e.staff_type == "delivery" or e.secondary_staff_type == "delivery"]

        # ── Per-staff leaderboard with month + all-time stats ──
        leaderboard = []
        for emp in delivery_staff:
            emp_trips   = [t for t in all_trips if t.emp_id == emp.id and t.status == "completed"]
            month_trips = [t for t in emp_trips if t.trip_date and t.trip_date >= month_start]
            total       = len(emp_trips)
            on_time     = sum(1 for t in emp_trips if t.is_on_time)
            month_total = len(month_trips)
            month_score = sum(score_delivery_trip(t) for t in month_trips)
            all_durs    = [t.duration_minutes for t in emp_trips if t.duration_minutes]
            avg_dur     = round(sum(all_durs) / len(all_durs), 1) if all_durs else None
            today_trips = [t for t in emp_trips if t.trip_date == today]
            leaderboard.append({
                "emp": emp, "total": total, "on_time": on_time,
                "on_time_pct": round(on_time / max(total, 1) * 100, 1),
                "month_total": month_total, "month_score": month_score,
                "avg_dur": avg_dur, "today_count": len(today_trips),
            })
        leaderboard.sort(key=lambda x: x["month_score"], reverse=True)

        # ── Daily delivery counts for last 14 days (chart data) ──
        from collections import defaultdict
        daily_counts = defaultdict(int)
        daily_ontime = defaultdict(int)
        for t in all_trips:
            if t.status == "completed" and t.trip_date and t.trip_date >= (today - timedelta(days=13)):
                day_str = t.trip_date.strftime("%d %b")
                daily_counts[day_str] += 1
                if t.is_on_time:
                    daily_ontime[day_str] += 1
        chart_labels = [(today - timedelta(days=i)).strftime("%d %b") for i in range(13, -1, -1)]
        chart_total  = [daily_counts.get(d, 0) for d in chart_labels]
        chart_ontime = [daily_ontime.get(d, 0) for d in chart_labels]

        # ── Overall summary stats ──
        completed_trips = [t for t in all_trips if t.status == "completed"]
        total_completed = len(completed_trips)
        total_on_time   = sum(1 for t in completed_trips if t.is_on_time)
        today_completed = sum(1 for t in completed_trips if t.trip_date == today)
        month_completed = sum(1 for t in completed_trips if t.trip_date and t.trip_date >= month_start)
        pending_count   = sum(1 for a in assignments if a.status == "pending")
        in_transit_count= sum(1 for a in assignments if a.status == "in_transit")

        return render_template("admin_delivery.html",
                               assignments=assignments, trip_map=trip_map,
                               emp_map=emp_map, leaderboard=leaderboard,
                               today=today, current_month=today.strftime("%B %Y"),
                               chart_labels=chart_labels,
                               chart_total=chart_total,
                               chart_ontime=chart_ontime,
                               total_completed=total_completed,
                               total_on_time=total_on_time,
                               today_completed=today_completed,
                               month_completed=month_completed,
                               pending_count=pending_count,
                               in_transit_count=in_transit_count)
    except Exception as e:
        logger.error(f"admin_delivery: {e}")
        flash("Error loading delivery dashboard.", "danger")
        return redirect(url_for("admin_dashboard"))


# ─── APScheduler Setup ────────────────────────────────────────────────────────

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        send_monthly_reports,
        trigger="cron",
        day=1, hour=8, minute=0,
        id="monthly_pdf_report",
        replace_existing=True
    )
    # Feature 16: archive ZIP on 1st of each month at 8:05 AM
    _scheduler.add_job(
        generate_monthly_report_job,
        trigger="cron",
        day=1, hour=8, minute=5,
        id="monthly_archive_report",
        replace_existing=True
    )
    # Feature 17: daily stale validations SMS at 9 AM
    _scheduler.add_job(
        stale_validations_sms_job,
        trigger="cron",
        hour=9, minute=0,
        id="stale_validations_sms",
        replace_existing=True
    )
    if not _scheduler.running:
        _scheduler.start()
    logger.info("APScheduler started — monthly reports job scheduled.")
except Exception as _sch_err:
    logger.warning(f"APScheduler init failed: {_sch_err}")
