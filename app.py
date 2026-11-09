import eventlet
eventlet.monkey_patch()

import os
import logging
import math
import time
import re
import io
import csv
import json
import random
import string
import requests as _requests
from datetime import date, timedelta, datetime
from collections import Counter
from functools import wraps
from typing import List, Optional, Dict, Any

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, jsonify, g, has_request_context
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
    # The fallback key is public (it's in the repo) — anyone could forge admin
    # session cookies with it. Use a random key instead: sessions reset on each
    # deploy until SECRET_KEY is set, but cookies can no longer be forged.
    import secrets as _secrets
    _secret_key = _secrets.token_hex(32)
    logger.critical("SECRET_KEY env var not set! Using a random key — all users are "
                    "logged out on every restart. Set SECRET_KEY in Render to fix.")

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
    logger.warning("DATABASE_URL not set! Using SQLite (local development mode)")
    db_url = "sqlite:///pharma_final.db"
else:
    # Supabase and Heroku-style URLs use the legacy "postgres://" scheme.
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    # Name the driver explicitly. A bare "postgresql://" lets SQLAlchemy pick,
    # and SQLAlchemy 2.1 changed that default from psycopg2 to psycopg (v3).
    # This project ships psycopg2-binary, so once SQLAlchemy reached 2.1 the app
    # died at boot with "No module named 'psycopg'" and every deploy failed --
    # without a line of app code changing. Pinning the driver in the URL makes
    # the choice ours rather than a default that can move under us.
    if db_url.startswith("postgresql://"):
        try:
            import psycopg2  # noqa: F401
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        except ImportError:
            try:
                import psycopg  # noqa: F401
                db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
            except ImportError:
                logger.critical("No PostgreSQL driver installed. Add psycopg2-binary "
                                "to requirements.txt.")

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
logger.info(f"Using database: {db_url.split('@')[0] if '@' in db_url else 'SQLite (local)'}")

db = SQLAlchemy(app)
mail = Mail(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ─── ROLES ───────────────────────────────────────────────────────────────────
# "biller" is internally kept but displayed as "Assigner". "supervisor" is a
# hidden role (admin-assigned only, never on the login screen).
VALID_STAFF_TYPES = ("picker", "checker", "purchaser", "delivery", "biller", "packer", "supervisor",
                     "pick_assigner")

# ─── Bill-based picking ─────────────────────────────────────────────────────
# A Pick Assigner adds each bill as it is made (bill number + items). Free
# pickers grab items from it; several pickers share one bill. Every grab gets
# a timer of items x PICK_SECONDS_PER_ITEM. Done on time earns +1 per item;
# late costs -1 per started minute over, never more than PICK_LATE_CAP.
PICK_SECONDS_PER_ITEM = max(5, int(os.environ.get("PICK_SECONDS_PER_ITEM", "30")))
PICK_URGENT_SECONDS = 20          # per item on an urgent bill
PICK_EXTRA_SECONDS = 125          # after normal time: first half find, second half report only
PICK_IDLE_SECONDS = 120           # a free picker has this long to take a waiting bill
PICK_URGENT_POOL = 10.0           # bonus points per urgent bill, shared by items picked
PICK_ONTIME_RATE = 1.5            # points per item finished within normal time
PICK_LATE_CAP = 5

# Hourly volume targets used by the scoring formulas. Set them to what this
# warehouse can actually reach: a target nobody attains makes those points dead
# weight and flattens everyone onto the same grade. tools_recommend_targets.py
# derives a demanding-but-reachable value from real history.
DEFAULT_PICKER_TARGET_HR  = float(os.environ.get("PICKER_TARGET_HR", "50"))
DEFAULT_CHECKER_TARGET_HR = float(os.environ.get("CHECKER_TARGET_HR", "70"))

# An urgent bill is harder than a normal one: it interrupts whatever the checker
# was doing, it has a customer waiting on it, and it cannot be batched. Volume
# used to count urgent and normal identically, so a checker who absorbed all the
# urgent work scored the same as one who cherry-picked easy normal bills. Urgent
# work is now credited at this multiple when volume is scored. 1.5 means one
# urgent bill counts as one and a half normal ones.
URGENT_BILL_WEIGHT = max(1.0, float(os.environ.get("URGENT_BILL_WEIGHT", "1.5")))

# ─── PRIVACY SETTINGS ────────────────────────────────────────────────────────
# Location is the most sensitive data here. Collect the least that still shows
# a useful route, and keep it only as long as it is needed to review deliveries.
GPS_RETENTION_DAYS = int(os.environ.get("GPS_RETENTION_DAYS", "30"))
GPS_MIN_SECONDS    = int(os.environ.get("GPS_MIN_SECONDS", "25"))
GPS_MIN_METRES     = int(os.environ.get("GPS_MIN_METRES", "40"))
GPS_MAX_POINTS     = int(os.environ.get("GPS_MAX_POINTS", "1200"))


def redact_email(addr):
    """Log-safe form of an email: keeps enough to debug, not enough to identify.
    Login attempts used to be logged with the full address, which puts a staff
    member's personal email into every log aggregator and error report the host
    forwards to.
    """
    addr = (addr or "").strip()
    if "@" not in addr:
        return "(none)" if not addr else (addr[:1] + "***")
    local, _, domain = addr.partition("@")
    head = local[:1] if local else ""
    return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"


# ─── CSRF PROTECTION ─────────────────────────────────────────────────────────
# Without this, any page on the internet can make a logged-in admin's browser
# submit a form here -- deleting an employee, awarding points, opening a past
# entry window -- because the session cookie is sent automatically. SameSite=Lax
# blocks the obvious cross-site POST, but it is one browser setting standing
# alone, and it does nothing against a same-site injection. A per-session token
# that the attacker's page cannot read is the actual defence.
#
# Implemented directly rather than via Flask-WTF: this app's forms are
# hand-written HTML, so a token helper and one before_request check cover them
# without adding a dependency or rewriting 49 forms into WTForms objects.

CSRF_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRFToken"
CSRF_EXEMPT = set()          # endpoint names that must not be checked


def csrf_token():
    """The current session's token, minted on first use."""
    tok = session.get("_csrf")
    if not tok:
        import secrets as _s
        tok = _s.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def csrf_exempt(view):
    """Mark a view as not requiring a token (machine callers, health checks)."""
    CSRF_EXEMPT.add(view.__name__)
    return view


@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return None
    if request.endpoint in CSRF_EXEMPT or request.endpoint == "static":
        return None
    sent = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER)
    if not sent and request.is_json:
        sent = (request.get_json(silent=True) or {}).get(CSRF_FIELD)
    expected = session.get("_csrf")
    import hmac as _hmac
    if not expected or not sent or not _hmac.compare_digest(str(sent), str(expected)):
        logger.warning("CSRF token missing or wrong on %s %s (ip=%s)",
                       request.method, request.path, request.remote_addr)
        if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
            return jsonify({"ok": False, "error": "Session expired. Reload the page."}), 400
        flash("Your session expired for security. Please try that again.", "warning")
        return redirect(request.referrer or url_for("login"))
    return None


@app.context_processor
def _inject_csrf():
    """Makes csrf_token() callable from every template."""
    return {"csrf_token": csrf_token}

ROLE_DISPLAY_NAMES = {
    "picker": "Operations Picker",
    "checker": "Operations Checker",
    "purchaser": "Operations Purchaser",
    "delivery": "Delivery Staff",
    "biller": "Assigner",
    "packer": "Packer",
    "supervisor": "Supervisor",
    "pick_assigner": "Pick Assigner",
}
# Recommended Cuttack delivery areas (from the delivery-CRM Excel) + packet types
KNOWN_ROUTES = [
    "RANIHAT", "MANGALABAG", "COLLEGE SQUARE", "BADAMBADI", "LINK ROAD",
    "B.K ROAD", "JOBRA", "CHAULIGANJ", "JAGATPUR", "BUXI BAZAR", "BALU BAZAR",
    "BUS STAND", "PURIGHAT", "GANDARPUR", "KANIKA CHAK", "THOTIA SAHI",
    "SUBHADRA", "BIDANASI", "NAYA SARAK", "TULSIPUR", "SUTAHAT", "DOLAMUNDAI",
]
PACKET_TYPES = ["Poly Bag", "Box", "Carton", "Cold Chain", "Bulk"]


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
    last_seen_at  = db.Column(db.DateTime, nullable=True)   # last time on the picking screen
    custom_hourly_target = db.Column(db.Float, nullable=True)  # Override default role target (Phase 1 auto-raise)
    phone         = db.Column(db.String(20), nullable=True)   # Feature 15: phone for OTP reset
    secondary_staff_type = db.Column(db.String(20), nullable=True)  # optional 2nd role for multitasking (kept for compat)
    extra_roles   = db.Column(db.String(160), nullable=True)  # comma-separated extra roles a multitasker may log
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


class PersonalRecord(db.Model):
    """A staff member's best day, best week and best month, and their level.

    One row per person per period ("day", "week", "month"). `best_value` is
    the record to beat; `level` counts how many times it was beaten.
    `last_checked` is the end of the last period already judged, so the same
    day, week or month can never level someone up twice.
    """
    __tablename__ = "personal_records"
    id           = db.Column(db.Integer, primary_key=True)
    emp_id       = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    period       = db.Column(db.String(8), nullable=False)
    best_value   = db.Column(db.Float, default=0.0)
    best_start   = db.Column(db.Date, nullable=True)
    best_end     = db.Column(db.Date, nullable=True)
    last_checked = db.Column(db.Date, nullable=True)
    level        = db.Column(db.Integer, default=0)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("emp_id", "period", name="_pr_emp_period_uc"),)


class LevelEvent(db.Model):
    """One level-up: who, which period, the new record and the one it beat."""
    __tablename__ = "level_events"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    period     = db.Column(db.String(8), nullable=False)
    value      = db.Column(db.Float, default=0.0)
    previous   = db.Column(db.Float, default=0.0)
    start      = db.Column(db.Date, nullable=True)
    end        = db.Column(db.Date, nullable=True)
    level      = db.Column(db.Integer, default=1)
    unit       = db.Column(db.String(40), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ErrorLog(db.Model):
    """Errors that happened on the live app, so they can be seen and fixed.

    The app sends a crashed request back to the login page and many routes
    catch their own errors, so failures were invisible: a button "did
    nothing". Each distinct error (same place + same message) is one row per
    day with a count, so a loop cannot flood the table or the admin's inbox.
    """
    __tablename__ = "error_logs"
    id         = db.Column(db.Integer, primary_key=True)
    day        = db.Column(db.Date, nullable=False, index=True)
    first_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_at    = db.Column(db.DateTime, default=datetime.utcnow)
    count      = db.Column(db.Integer, default=1)
    path       = db.Column(db.String(200), default="")
    method     = db.Column(db.String(10), default="")
    user_id    = db.Column(db.Integer, nullable=True)
    user_name  = db.Column(db.String(120), default="")
    where      = db.Column(db.String(160), default="")
    message    = db.Column(db.String(500), default="")
    detail     = db.Column(db.Text, default="")
    alerted    = db.Column(db.Boolean, default=False)


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
    # Billing role params
    bills_done     = db.Column(db.Integer, default=0)
    items_count    = db.Column(db.Integer, default=0)
    # "Other" role params (breakage / expiry handling)
    breakage_received          = db.Column(db.Integer, default=0)
    expire_received            = db.Column(db.Integer, default=0)
    breakage_expire_processed  = db.Column(db.Integer, default=0)
    item_receive_qty           = db.Column(db.Integer, default=0)
    expire_return_qty          = db.Column(db.Integer, default=0)
    note           = db.Column(db.String(300), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "entry_date", "secondary_type", name="_mt_emp_date_role_uc"),
        db.Index("idx_mt_emp_date", "emp_id", "entry_date"),
    )

    @property
    def summary(self):
        """Human-readable summary of the figures this entry actually carries.

        Every role's full field list used to be printed whether or not the
        numbers were there, so the widest column in the admin panel was often
        a row of zeros -- "PO Bills Rcvd: 0 · PO Checked: 0 · PO Entry: 0 ·
        Items: 0 · CS Open: 0 · CS Rcvd: 0" -- which says nothing and crowds
        out the entries that do carry work. Fields at zero are dropped; an
        entry with nothing in it says so plainly.
        """
        st = self.secondary_type
        fields = {
            "picker": [("Bills Rcvd", self.total_bills_received),
                       ("SB Picked", self.sales_bills_open),
                       ("Items Picked", self.picked), ("Missed", self.missed),
                       ("CS Open", self.cs_sales_open), ("Packing", self.packing_done)],
            "checker": [("Bills Rcvd", self.bills_received),
                        ("SB Checked", self.sales_bills_open),
                        ("CS Checked", self.cs_sales_open),
                        ("Items Checked", self.checked), ("Urgent", self.errors_found)],
            "purchaser": [("PO Bills Rcvd", self.sales_bills_open),
                          ("PO Checked", self.checked), ("PO Entry", self.picked),
                          ("Items", self.errors_found), ("CS Open", self.cs_sales_open),
                          ("CS Rcvd", self.packing_done)],
            "billing": [("Bills Done", self.bills_done), ("Items", self.items_count),
                        ("Qty", self.quantity)],
            "other": [("Brkg Rcvd", self.breakage_received),
                      ("Exp Rcvd", self.expire_received),
                      ("Processed", self.breakage_expire_processed),
                      ("Item Recv", self.item_receive_qty),
                      ("Exp Return", self.expire_return_qty)],
        }.get(st, [("Qty Done", self.quantity)])
        shown = [f"{label}: {value}" for label, value in fields if value]
        return " · ".join(shown) if shown else "No figures entered"


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
    table_clean  = db.Column(db.Integer, default=0)   # 0=No 1=Yes (workspace — for everyone)
    sweep_done   = db.Column(db.Integer, default=0)   # 0=No 1=Yes (workspace — for everyone)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def figures(self):
        """The two numbers in the words the form used for THIS role. A packer's
        form asks for "Sales Bill Open" and "Total Bills Received", stored in
        the same two columns a driver uses for deliveries and issues -- so
        printing "processed / issues" for everyone mislabelled packers."""
        q, n = self.quantity or 0, self.issues_count or 0
        if self.note_type == "packer":
            return "%d sales bills open · %d bills received" % (q, n)
        if self.note_type == "delivery":
            return "%d deliveries · %d issues" % (q, n)
        return "%d bills processed · %d issues" % (q, n)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "entry_date", name="_dbnote_emp_date_uc"),
        db.Index("idx_dbn_date", "entry_date"),
    )

    @property
    def workspace_score(self):
        """Simple workspace points: 5 each for table clean + sweep done = 10 max."""
        return (5 if self.table_clean else 0) + (5 if self.sweep_done else 0)


class DeliveryAssignment(db.Model):
    """Assigner dispatches a route (one or more areas) to a delivery employee.
    Mirrors the real delivery-CRM Excel: ROUTE, NO OF TASK, DISPATCH TIME, returns, etc."""
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
    # ── Excel-CRM fields ──
    route            = db.Column(db.String(300), nullable=True)   # comma-separated areas e.g. "RANIHAT,MANGALABAG"
    no_of_tasks      = db.Column(db.Integer, default=1)            # NO OF TASK (packets in this dispatch)
    packet_type      = db.Column(db.String(50), nullable=True)    # Poly Bag / Box / Carton / Fragile ...
    dispatch_time    = db.Column(db.DateTime, nullable=True)       # DISPATCH TIME (defaults to assigned_at)
    no_of_task_return= db.Column(db.Integer, default=0)           # NO OF TASK RETURN
    return_reason    = db.Column(db.String(200), nullable=True)   # RETURN REASON (e.g. CLOSE)
    __table_args__ = (
        db.Index("idx_da_delivery_emp", "delivery_emp_id"),
        db.Index("idx_da_status", "status"),
    )


class DeliveryStop(db.Model):
    """A single stop within a delivery trip — the delivery boy logs each place reached.
    Packages across all stops sum to the trip's delivered total; per-stop timestamps give timing."""
    __tablename__ = "delivery_stops"
    id            = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("delivery_assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    emp_id        = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    place_name    = db.Column(db.String(150), nullable=False)
    area          = db.Column(db.String(150), nullable=True)   # delivery area (RANIHAT, MANGALABAG …)
    packages      = db.Column(db.Integer, default=0)          # packets delivered at this stop
    reached_at    = db.Column(db.DateTime, nullable=True)     # NULL until the rider logs arrival
    minutes_from_prev = db.Column(db.Float, nullable=True)    # time since previous stop / departure
    lat           = db.Column(db.Float, nullable=True)
    lng           = db.Column(db.Float, nullable=True)
    note          = db.Column(db.String(300), nullable=True)
    packet_type   = db.Column(db.String(50), nullable=True)   # per-store packet type (Box / Poly Bag / Fragile ...)
    dist_from_prev_km = db.Column(db.Float, nullable=True)    # straight-line km from previous stop / store base


class Notification(db.Model):
    """In-app notification (bell). Used to alert delivery staff of new assignments, etc."""
    __tablename__ = "notifications"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    title      = db.Column(db.String(150), nullable=False)
    body       = db.Column(db.String(400), nullable=True)
    link       = db.Column(db.String(200), nullable=True)
    is_read    = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PushSub(db.Model):
    """A phone or browser that asked for alerts (Web Push subscription)."""
    __tablename__ = "push_subs"
    id         = db.Column(db.Integer, primary_key=True)
    emp_id     = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint   = db.Column(db.String(600), nullable=False, index=True)
    p256dh     = db.Column(db.String(200), nullable=False)
    auth       = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AppKey(db.Model):
    """Small server-side values kept in the database (the push key pair)."""
    __tablename__ = "app_keys"
    name  = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(400), nullable=False)


class PickBill(db.Model):
    """A bill added by the Pick Assigner for pickers to pick."""
    __tablename__ = "pick_bills"
    id           = db.Column(db.Integer, primary_key=True)
    bill_no      = db.Column(db.String(40), nullable=False)
    items        = db.Column(db.Integer, nullable=False)
    created_by   = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    status       = db.Column(db.String(12), default="open", index=True)   # open / done / cancelled / scheduled
    is_urgent    = db.Column(db.Boolean, default=False)
    scheduled_for = db.Column(db.DateTime, nullable=True)   # rescheduled bills come back at this time


class PickClaim(db.Model):
    """Items of one bill grabbed by one picker, with its own timer."""
    __tablename__ = "pick_claims"
    id         = db.Column(db.Integer, primary_key=True)
    bill_id    = db.Column(db.Integer, db.ForeignKey("pick_bills.id", ondelete="CASCADE"), nullable=False, index=True)
    picker_id  = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    items      = db.Column(db.Integer, nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_at     = db.Column(db.DateTime, nullable=False)
    done_at    = db.Column(db.DateTime, nullable=True)
    work_date  = db.Column(db.Date, nullable=True, index=True)   # local day the grab was finished
    status     = db.Column(db.String(12), default="active", index=True)  # active / done / cancelled
    points     = db.Column(db.Float, default=0.0)
    bonus      = db.Column(db.Float, default=0.0)       # share of an urgent bill's pool
    picked     = db.Column(db.Integer, nullable=True)   # items actually picked (rest reported missing)
    missing    = db.Column(db.Integer, default=0)
    outcome    = db.Column(db.String(12), nullable=True)  # ontime / extra / report / late
    late_alerted = db.Column(db.Boolean, default=False)   # assigner told it ran late


class PickMissing(db.Model):
    """An item a picker could not find on a bill."""
    __tablename__ = "pick_missing"
    id          = db.Column(db.Integer, primary_key=True)
    bill_id     = db.Column(db.Integer, db.ForeignKey("pick_bills.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_id    = db.Column(db.Integer, db.ForeignKey("pick_claims.id", ondelete="CASCADE"), nullable=True)
    picker_id   = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=True, index=True)
    item_name   = db.Column(db.String(120), nullable=False)
    qty         = db.Column(db.Integer, default=1)
    reported_at = db.Column(db.DateTime, default=datetime.utcnow)
    status      = db.Column(db.String(12), default="open", index=True)  # open / reassigned / unavailable
    handled_at  = db.Column(db.DateTime, nullable=True)


class PickDay(db.Model):
    """A picking day closed by the Pick Assigner: its total bills become the
    checkers' day total (fixed)."""
    __tablename__ = "pick_days"
    id          = db.Column(db.Integer, primary_key=True)
    day         = db.Column(db.Date, unique=True, index=True, nullable=False)
    total_bills = db.Column(db.Integer, nullable=False)
    closed_by   = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    closed_at   = db.Column(db.DateTime, default=datetime.utcnow)


class PickEvent(db.Model):
    """The day's late, idle and missing-item log for the assigner and admin."""
    __tablename__ = "pick_events"
    id        = db.Column(db.Integer, primary_key=True)
    kind      = db.Column(db.String(12), nullable=False, index=True)   # late / idle / missing
    picker_id = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=True, index=True)
    bill_id   = db.Column(db.Integer, db.ForeignKey("pick_bills.id", ondelete="CASCADE"), nullable=True)
    at        = db.Column(db.DateTime, default=datetime.utcnow)
    day       = db.Column(db.Date, index=True)
    minutes   = db.Column(db.Float, nullable=True)
    detail    = db.Column(db.String(200), nullable=True)


class ChecklistTemplate(db.Model):
    """A daily task the admin defines for a whole staff type.

    Every employee of that staff_type sees these items on their dashboard each
    day and ticks them off. Items are per-role rather than per-person so adding
    a new employee needs no extra setup, and so completion is comparable between
    staff doing the same job.
    """
    __tablename__ = "checklist_templates"
    id          = db.Column(db.Integer, primary_key=True)
    staff_type  = db.Column(db.String(20), nullable=False, index=True)
    text        = db.Column(db.String(200), nullable=False)
    sort_order  = db.Column(db.Integer, default=0)
    is_active   = db.Column(db.Integer, default=1)   # 0 archived, 1 live
    created_by  = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.Index("idx_cl_tmpl_role", "staff_type", "is_active"),
    )


class ChecklistCompletion(db.Model):
    """One employee ticking one checklist item on one day.

    A row exists only once an item has been ticked at least once; un-ticking
    sets done=0 rather than deleting, so the audit trail of when it was first
    completed survives.
    """
    __tablename__ = "checklist_completions"
    id           = db.Column(db.Integer, primary_key=True)
    emp_id       = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    template_id  = db.Column(db.Integer, db.ForeignKey("checklist_templates.id", ondelete="CASCADE"), nullable=False)
    entry_date   = db.Column(db.Date, nullable=False)
    done         = db.Column(db.Integer, default=0)
    completed_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.UniqueConstraint("emp_id", "template_id", "entry_date", name="_cl_emp_tmpl_date_uc"),
        db.Index("idx_cl_done_date", "entry_date", "emp_id"),
    )


class SupervisorBill(db.Model):
    """Authoritative ('super') bill count entered by a supervisor for a staff member on a date.
    Admin-validated source of truth; staff self-reports are checked against this."""
    __tablename__ = "supervisor_bills"
    id            = db.Column(db.Integer, primary_key=True)
    staff_id      = db.Column(db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    entry_date    = db.Column(db.Date, nullable=False, index=True)
    super_bills   = db.Column(db.Integer, default=0)        # authoritative count
    supervisor_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    admin_validated = db.Column(db.Boolean, default=False)  # admin confirmed it's true
    note          = db.Column(db.String(200), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("staff_id", "entry_date", name="_supbill_staff_date_uc"),)


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


class BreadcrumbPoint(db.Model):
    """GPS breadcrumb trail point recorded during an active delivery trip."""
    __tablename__ = "breadcrumb_points"
    id          = db.Column(db.Integer, primary_key=True)
    trip_id     = db.Column(db.Integer, db.ForeignKey("delivery_trips.id", ondelete="CASCADE"), nullable=False)
    lat         = db.Column(db.Float, nullable=False)
    lng         = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.Index("idx_bc_trip", "trip_id", "recorded_at"),
    )


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def safe_div(a, b, default=0.0):
    try: return a / b if b else default
    except Exception: return default


def req_memo(name, key, fn):
    """Answer the same lookup once per page load.

    Building the admin dashboard runs the scoring for every staff member three
    times (today, week, month), and each run used to ask the database the same
    three questions again -- open complaints, supervisor strikes, multitask
    entries -- 83 of the page's 97 queries. Outside a request (scheduled jobs)
    it simply calls fn().
    """
    if not has_request_context():
        return fn()
    store = getattr(g, "_memo_" + name, None)
    if store is None:
        store = {}
        setattr(g, "_memo_" + name, store)
    if key not in store:
        store[key] = fn()
    return store[key]


def invalidate_request_memos():
    """Forget per-request answers after a write, so a page built in the same
    request after saving sees the new data."""
    if not has_request_context():
        return
    for k in [k for k in vars(g) if k.startswith("_memo_") or k == "_checker_team_cache"]:
        delattr(g, k)


class _ErrorCapture(logging.Handler):
    """Records every ERROR the app logs into error_logs, with the page and
    user it happened for, and emails the admin the first time each distinct
    error appears in a day.

    Written over its own connection, never the request's session: a request
    that failed may have a half-finished transaction, and committing the log
    through the same session would commit that half-finished work too.
    """
    _busy = False

    def emit(self, record):
        if _ErrorCapture._busy or record.levelno < logging.ERROR:
            return
        _ErrorCapture._busy = True
        try:
            msg = record.getMessage()[:500]
            if "error_logs" in msg:            # never log failures of the log itself
                return
            import traceback as _tb
            detail = ""
            if record.exc_info and record.exc_info[0]:
                detail = "".join(_tb.format_exception(*record.exc_info))[-4000:]
            path = method = uname = ""
            uid = None
            if has_request_context():
                path, method = request.path[:200], request.method
                uid = session.get("user_id")
                uname = (session.get("user_name") or "")[:120]
            where = ("%s:%s" % (record.funcName, record.lineno))[:160]
            now = datetime.utcnow()
            today = now.date()
            tbl = ErrorLog.__table__
            with db.engine.begin() as conn:
                row = conn.execute(tbl.select().where(
                    (tbl.c.day == today) & (tbl.c.where == where) & (tbl.c.message == msg)
                    & (tbl.c.path == path))).first()
                if row:
                    conn.execute(tbl.update().where(tbl.c.id == row.id)
                                 .values(count=tbl.c.count + 1, last_at=now))
                    return
                conn.execute(tbl.insert().values(
                    day=today, first_at=now, last_at=now, count=1, path=path, method=method,
                    user_id=uid, user_name=uname, where=where, message=msg, detail=detail,
                    alerted=False))
            _alert_admin_of_error(path, uname, msg, detail)
        except Exception:
            pass
        finally:
            _ErrorCapture._busy = False


def _alert_admin_of_error(path, user, msg, detail):
    """Email the owner about a NEW error, in the background so the page that
    failed is not slowed down. Silently does nothing if mail is not set up."""
    to = report_recipient()
    if not to or active_email_transport() is None or app.config.get("TESTING"):
        return
    body = ("A new error happened on the Pharma IP System.\n\n"
            "Page: %s\nUser: %s\nError: %s\n\n"
            "Every error is listed on Admin > More > Diagnostics, with a count of how often "
            "it happened. Send the details to your developer to fix.\n\n%s"
            % (path or "(background job)", user or "-", msg, detail[-1500:]))
    def _go():
        try:
            send_test_email(to, "Pharma IP System - new error: %s" % msg[:80], body)
        except Exception:
            pass
    try:
        import threading
        threading.Thread(target=_go, daemon=True).start()
    except Exception:
        pass


logger.addHandler(_ErrorCapture())


def recent_errors(days=7, limit=50):
    try:
        since = date.today() - timedelta(days=days)
        return (ErrorLog.query.filter(ErrorLog.day >= since)
                .order_by(ErrorLog.last_at.desc()).limit(limit).all())
    except Exception:
        return []


def checker_team_day_totals(dates):
    """Team-wide checker bill figures for each of `dates`.

    Pending bills are a TEAM number, not a personal one. Every checker on duty
    types in the same figure for the day's total sales bills, then how many of
    them they personally got through (urgent and normal). What is left over at
    the end of the day is the day's total minus what the WHOLE team cleared --
    which no single checker's row can tell you.

        Lisa, Smriti and Deepti each enter "total bills: 100".
        Lisa clears 30 (urgent + normal), Smriti 30, Deepti 30.
        Pending for the day = 100 - (30 + 30 + 30) = 10.

    This used to be a number each checker typed in by hand
    (`pending_bills_manual`), so three checkers on one day produced three
    different, usually wrong, answers and they were then summed -- inflating
    pending threefold and dragging every checker's clearance score down with it.

    The day's total is taken as the MAX of what the checkers entered rather than
    the sum, because they are all reporting the same shared figure; one person
    leaving it blank must not shrink the day's total.

    Returns {date: {"total", "checked", "normal", "urgent", "pending",
                    "by_emp": {emp_id: checked}}}.
    """
    out = {}
    dates = sorted({d for d in dates if d})
    if not dates:
        return out

    # One request can build analytics for every checker on the roster, and they
    # all want the same team figures. Memoise per request so the roster costs
    # two queries, not two per checker.
    cache_key = tuple(dates)
    if has_request_context():
        cache = getattr(g, "_checker_team_cache", None)
        if cache is None:
            cache = g._checker_team_cache = {}
    else:
        cache = None           # a scheduled job or a script: no request to scope to
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    for d in dates:
        out[d] = {"total": 0, "checked": 0, "normal": 0, "urgent": 0,
                  "pending": 0, "by_emp": {}, "entered": {}, "mode": "team"}

    def _absorb(d, emp_id, total, normal, urgent):
        row = out.get(d)
        if row is None:
            return
        row["total"] = max(row["total"], int(total or 0))
        row["entered"][emp_id] = max(row["entered"].get(emp_id, 0), int(total or 0))
        row["normal"] += int(normal or 0)
        row["urgent"] += int(urgent or 0)
        n = int(normal or 0) + int(urgent or 0)
        row["checked"] += n
        row["by_emp"][emp_id] = row["by_emp"].get(emp_id, 0) + n

    try:
        # Checkers working their primary role.
        rows = (db.session.query(KPIEntry, Employee)
                .join(Employee, Employee.id == KPIEntry.emp_id)
                .filter(KPIEntry.entry_date.in_(dates),
                        Employee.staff_type == "checker")
                .all())
        for e, _emp in rows:
            _absorb(e.entry_date, e.emp_id, e.bills_received,
                    e.sales_bills_open or e.bills or 0, e.cs_sales_open)

        # Anyone who checked as a SECOND role that day cleared real bills too,
        # so their work has to come off the same pending pile.
        for m in (MultitaskEntry.query
                  .filter(MultitaskEntry.entry_date.in_(dates),
                          MultitaskEntry.secondary_type == "checker").all()):
            _absorb(m.entry_date, m.emp_id, m.bills_received,
                    m.sales_bills_open, m.cs_sales_open)
    except Exception as exc:
        # Scoring depends on this, so it must not fail quietly. With no team
        # figures the page shows "0 total - 0 checked", which is visibly
        # wrong rather than a flattering clearance score.
        logger.error("checker_team_day_totals failed: %s", exc)
        return out

    for d_, row in out.items():
        # A day closed by the Pick Assigner has a fixed total: the number of
        # bills actually assigned that day. It overrides what was typed.
        _fixed = pick_day_total(d_)
        if _fixed is not None:
            row["total"], row["mode"] = _fixed, "assigned"
            row["pending"] = max(row["total"] - row["checked"], 0)
            continue
        # The form used to say "All bills given to YOU today", so on those days
        # each checker typed their own bills, not the day's total. You can tell:
        # the team checked more bills than the biggest "total" anyone entered.
        # On such a day the only reading that adds up is that each figure was
        # that checker's own share, so the day's total is their sum.
        if row["checked"] > row["total"]:
            entered = [v for v in row["entered"].values() if v > 0]
            summed = sum(entered)
            # Only if the checkers typed DIFFERENT figures can they have been
            # their own shares. If everyone typed the same number it was the
            # day's total, and adding them up would multiply the day by the
            # head-count (5 x 129 = 645) -- so that is flagged instead.
            if len(set(entered)) > 1 and summed >= row["checked"]:
                row["total"], row["mode"] = summed, "per_person"
            else:
                row["mode"] = "conflict"      # no reading of the figures adds up
        row["pending"] = max(row["total"] - row["checked"], 0)
    if cache is not None:
        cache[cache_key] = out
    return out


def entry_problems(staff_type, v):
    """Why a day's figures cannot be true. Empty list means they add up.

    The owner's rule -- if the total is 100, the parts cannot come to more
    than 100 -- applied to every role, not only checkers' bills:
      picker     bills picked <= bills given; items picked >= bills picked
      checker    items checked >= bills checked (every bill has an item)
      purchaser  PO checked <= PO received; PO entered <= PO received;
                 items racked <= items; CS received <= CS open
    `v` holds the KPIEntry field names. Team-wide checker bills are checked
    separately by checker_bill_problems().
    """
    g_ = lambda k: int(v.get(k) or 0)
    out = []
    if staff_type == "picker":
        if g_("total_bills_received") and g_("sales_bills_open") > g_("total_bills_received"):
            out.append("Bills picked (%d) is more than bills given to pick (%d)."
                       % (g_("sales_bills_open"), g_("total_bills_received")))
        if g_("sales_bills_open") and g_("picked") + g_("missed") < g_("sales_bills_open"):
            out.append("%d bills picked but only %d items -- every bill has at least one item."
                       % (g_("sales_bills_open"), g_("picked") + g_("missed")))
    elif staff_type == "checker":
        bills = g_("sales_bills_open") + g_("cs_sales_open")
        items = g_("checked") + g_("errors_found")
        if bills and items < bills:
            out.append("%d bills checked but only %d items -- every bill has at least one item."
                       % (bills, items))
    elif staff_type == "purchaser":
        rec = g_("sales_bills_open")
        if rec and g_("checked") > rec:
            out.append("PO bills checked (%d) is more than PO bills received (%d)." % (g_("checked"), rec))
        if rec and g_("picked") > rec:
            out.append("PO bills entered (%d) is more than PO bills received (%d)." % (g_("picked"), rec))
        if g_("errors_found") and g_("rack_organized") > g_("errors_found"):
            out.append("Items racked (%d) is more than the number of items (%d)."
                       % (g_("rack_organized"), g_("errors_found")))
        if g_("cs_sales_open") and g_("packing_done") > g_("cs_sales_open"):
            out.append("CS received (%d) is more than CS open (%d)." % (g_("packing_done"), g_("cs_sales_open")))
    return out


def checker_bill_problems(entry_date, emp_id, bills_received, sb_checked, cs_checked):
    """Why a checker's bill figures cannot be right. Empty list means they add up.

    Urgent bills are part of the day's total, not extra to it. If the day had
    100 sales bills then normal + urgent checked can never come to more than
    100. Nothing stopped that before, so a day could show 130 of 100 bills
    cleared and a negative pending pile.

    The same ceiling applies to the whole checking team: everyone's checked
    bills together cannot exceed the day's total. That is safe to enforce
    because the day's total is the HIGHEST figure any checker entered -- one
    person typing too low a total can never block a colleague who types the
    right one.
    """
    problems = []
    bills_received = int(bills_received or 0)
    sb_checked     = int(sb_checked or 0)
    cs_checked     = int(cs_checked or 0)
    mine = sb_checked + cs_checked

    if bills_received > 0 and mine > bills_received:
        problems.append(
            "Bills checked (%d normal + %d urgent = %d) is more than the day's "
            "total sales bills (%d). Urgent bills are part of that total, not "
            "extra to it." % (sb_checked, cs_checked, mine, bills_received))

    try:
        if has_request_context():
            g._checker_team_cache = {}
        day = checker_team_day_totals([entry_date]).get(entry_date) or {}
        others = sum(v for eid, v in (day.get("by_emp") or {}).items() if eid != emp_id)
        day_total = max(int(day.get("total") or 0), bills_received)
        if day_total > 0 and others + mine > day_total and mine <= bills_received:
            problems.append(
                "The checking team would be at %d bills out of the day's %d "
                "(%d already entered by other checkers + %d here). Only %d "
                "bills are left to check today -- or correct the day's total if it is wrong."
                % (others + mine, day_total, others, mine, max(day_total - others, 0)))
    except Exception as exc:
        logger.warning("checker_bill_problems team check failed: %s", exc)
    return problems


def checker_bill_warnings(entry_date, emp_id, bills_received, sb_checked, cs_checked):
    """Why a checker's figures look wrong against the rest of the team's.

    The same ceiling applies to the team as a whole -- the day's bills cannot
    all be checked twice -- but this is deliberately NOT a refusal. If one
    person mistypes the day's total, everyone who enters after them would be
    locked out of submitting at all, which is worse than a wrong figure. So it
    is said out loud and the entry is still saved.
    """
    warnings = []
    mine = int(sb_checked or 0) + int(cs_checked or 0)
    try:
        # Validation runs around writes, so the per-request memo may be stale.
        if has_request_context():
            g._checker_team_cache = {}
        day = checker_team_day_totals([entry_date]).get(entry_date) or {}
        others = sum(v for eid, v in (day.get("by_emp") or {}).items() if eid != emp_id)
        day_total = max(int(day.get("total") or 0), int(bills_received or 0))
        if day_total > 0 and others + mine > day_total:
            warnings.append(
                "Saved, but the checking team is now at %d bills out of %d for "
                "this day (%d already entered by other checkers, %d here). "
                "Check the day's total, or whose bills these are."
                % (others + mine, day_total, others, mine))
    except Exception as exc:
        logger.warning("checker_bill_warnings team check failed: %s", exc)
    return warnings


# ─── MULTITASK REPORT ────────────────────────────────────────────────────────
# A fixed role order, used for both the slice colours and the legend. It is
# fixed on purpose: a colour follows the ROLE, never its rank in one person's
# chart, so purple means "purchaser" on every card on the page and a staff
# member who stops doing a role does not repaint everyone else's slices.
MULTITASK_ROLE_ORDER = ("picker", "checker", "purchaser", "billing",
                        "delivery", "packing", "other")

# One number per role that means something on the shop floor, so a slice can be
# read as work done and not only as "a day".
MULTITASK_HEADLINE = {
    "picker":    ("items picked",    lambda m: int(m.picked or 0)),
    "checker":   ("bills checked",   lambda m: int(m.sales_bills_open or 0) + int(m.cs_sales_open or 0)),
    "purchaser": ("PO bills checked", lambda m: int(m.checked or 0)),
    "billing":   ("bills billed",    lambda m: int(m.bills_done or 0)),
    "delivery":  ("deliveries done", lambda m: int(m.quantity or 0)),
    "packing":   ("packs done",      lambda m: int(m.quantity or 0)),
    "other":     ("items processed", lambda m: int(m.breakage_expire_processed or 0)
                                               + int(m.item_receive_qty or 0)
                                               + int(m.expire_return_qty or 0)),
}


def multitask_role_label(role):
    return (role or "other").replace("_", " ").title()


def multitask_report(days=30):
    """Who multitasked, in which roles, over the last `days` days -- per person.

    The admin panel only ever had a flat list of every extra-role report in
    date order. With a handful of staff each covering two or three roles that
    is a wall of rows nobody reads, and it answers none of the questions worth
    asking: who is carrying the extra load, which roles are short-handed, and
    what did the cover actually produce.

    Returns a dict with:
      staff   -- one entry per person who covered an extra role, busiest first,
                 each with its own role split ready to draw
      roles   -- the same split for the warehouse as a whole
      by_role -- role -> the people covering it, so a role-wise view costs no
                 extra queries
      totals  -- headline figures
    Multitask points follow the same rule the score uses: 10 points per extra
    role on a day, capped at 30 for the day, shared evenly between that day's
    roles so the per-role figures still add up to the day's award.
    """
    out = {"staff": [], "roles": [], "by_role": {}, "days": days,
           "totals": {"people": 0, "role_days": 0, "points": 0.0, "roles": 0},
           "since": None}
    try:
        since = date.today() - timedelta(days=days)
        out["since"] = since
        entries = (MultitaskEntry.query
                   .filter(MultitaskEntry.entry_date >= since)
                   .order_by(MultitaskEntry.entry_date.desc()).all())
        if not entries:
            return out

        emps = {e.id: e for e in Employee.query.all()}

        # Points are awarded per DAY, so count each person's roles per day first.
        roles_per_day = {}
        for m in entries:
            roles_per_day.setdefault((m.emp_id, m.entry_date), set()).add(m.secondary_type)

        per_person = {}
        for m in entries:
            role = m.secondary_type or "other"
            day_roles = roles_per_day.get((m.emp_id, m.entry_date)) or {role}
            n = max(len(day_roles), 1)
            day_award = min(10 * n, 30)
            share = day_award / n          # so the roles still sum to the day

            p = per_person.setdefault(m.emp_id, {"roles": {}, "dates": set()})
            p["dates"].add(m.entry_date)
            r = p["roles"].setdefault(role, {"days": 0, "points": 0.0,
                                             "volume": 0, "notes": []})
            r["days"] += 1
            r["points"] += share
            _label, _fn = MULTITASK_HEADLINE.get(role, ("entries", lambda _m: 0))
            try:
                r["volume"] += _fn(m)
            except Exception:
                pass
            if m.note:
                r["notes"].append(m.note)

        def _slices(role_map, total_days):
            """Role rows in the fixed role order, with the share each holds."""
            rows = []
            for role in MULTITASK_ROLE_ORDER:
                r = role_map.get(role)
                if not r:
                    continue
                unit, _ = MULTITASK_HEADLINE.get(role, ("entries", None))
                rows.append({
                    "role": role,
                    "label": multitask_role_label(role),
                    "slot": MULTITASK_ROLE_ORDER.index(role) + 1,
                    "days": r["days"],
                    "points": round(r["points"], 1),
                    "volume": r["volume"],
                    "unit": unit,
                    "pct": round(safe_div(r["days"], total_days) * 100, 1),
                })
            # Roles the code does not know about must still be visible rather
            # than silently dropped out of the chart.
            for role, r in role_map.items():
                if role in MULTITASK_ROLE_ORDER:
                    continue
                rows.append({
                    "role": role, "label": multitask_role_label(role),
                    "slot": len(MULTITASK_ROLE_ORDER), "days": r["days"],
                    "points": round(r["points"], 1), "volume": r["volume"],
                    "unit": "entries",
                    "pct": round(safe_div(r["days"], total_days) * 100, 1),
                })
            return sorted(rows, key=lambda x: (-x["days"], x["label"]))

        wide_roles = {}
        for emp_id, p in per_person.items():
            total_role_days = sum(r["days"] for r in p["roles"].values())
            emp = emps.get(emp_id)
            out["staff"].append({
                "emp_id": emp_id,
                "name": emp.name if emp else "Employee #%d" % emp_id,
                "primary": multitask_role_label(emp.staff_type) if emp else "—",
                "secondary": (multitask_role_label(emp.secondary_staff_type)
                              if emp and emp.secondary_staff_type else None),
                "days": len(p["dates"]),
                "role_days": total_role_days,
                "role_count": len(p["roles"]),
                "points": round(sum(r["points"] for r in p["roles"].values()), 1),
                "slices": _slices(p["roles"], total_role_days),
            })
            for role, r in p["roles"].items():
                w = wide_roles.setdefault(role, {"days": 0, "points": 0.0,
                                                 "volume": 0, "notes": []})
                w["days"] += r["days"]
                w["points"] += r["points"]
                w["volume"] += r["volume"]

        out["staff"].sort(key=lambda s: (-s["role_days"], -s["points"], s["name"]))
        wide_total_days = sum(r["days"] for r in wide_roles.values())
        out["roles"] = _slices(wide_roles, wide_total_days)

        for s in out["staff"]:
            for sl in s["slices"]:
                out["by_role"].setdefault(sl["role"], []).append({
                    "emp_id": s["emp_id"], "name": s["name"],
                    "primary": s["primary"], "days": sl["days"],
                    "points": sl["points"], "volume": sl["volume"],
                    "unit": sl["unit"],
                })
        for role in out["by_role"]:
            out["by_role"][role].sort(key=lambda x: (-x["days"], x["name"]))

        out["totals"] = {
            "people": len(out["staff"]),
            "role_days": wide_total_days,
            "points": round(sum(r["points"] for r in wide_roles.values()), 1),
            "roles": len(wide_roles),
        }
    except Exception as exc:
        logger.error("multitask_report failed: %s", exc)
    return out


# ─── LEVEL-UP SYSTEM ─────────────────────────────────────────────────────────
# The owner's rule: when a staff member has a day better than good that is
# also their best day ever, that best day becomes their new target and they
# level up, with a congratulations notification. The same for best week and
# best month. Targets only ever go up.
LEVEL_UNIT = {"picker": "items/hr", "checker": "weighted items/hr", "purchaser": "items/hr",
              "delivery": "deliveries", "biller": "bills processed"}
LEVEL_SCORED = ("picker", "checker", "purchaser")   # these have a score and a target
LEVEL_GOOD_SCORE = 72.0       # "better than good": PROFICIENT or ELITE
LEVEL_MIN_HISTORY = {"day": 5, "week": 2, "month": 1}   # earlier periods needed before levels start
LEVEL_MIN_DAYS = {"day": 1, "week": 3, "month": 10}      # working days needed inside a period


def _level_day_value(role, e):
    """The day's figure for this role, from one KPI entry."""
    if role == "picker":
        # Bill-based picking: items per hour actually spent picking.
        return float(picker_pick_stats(e.emp_id, [e.entry_date])["daily_rate"].get(e.entry_date, 0.0))
    if role == "checker":
        return round(((e.checked or 0) + (e.errors_found or 0) * URGENT_BILL_WEIGHT) / 9.0, 1)
    if role == "purchaser":
        return round((e.errors_found or 0) / 9.0, 1)
    return 0.0


def _level_daily(emp, start, end):
    """{date: (value, kpi_entry_or_None)} for every working day in [start, end]."""
    role = emp.staff_type
    out = {}
    if role in LEVEL_SCORED:
        for e in KPIEntry.query.filter(KPIEntry.emp_id == emp.id, KPIEntry.entry_date >= start,
                                       KPIEntry.entry_date <= end).all():
            out[e.entry_date] = (_level_day_value(role, e), e)
    elif role in ("delivery", "biller"):
        for n in DeliveryBillerNote.query.filter(DeliveryBillerNote.emp_id == emp.id,
                                                 DeliveryBillerNote.entry_date >= start,
                                                 DeliveryBillerNote.entry_date <= end).all():
            v = out.get(n.entry_date, (0.0, None))[0] + float(n.quantity or 0)
            out[n.entry_date] = (v, None)
    return out


def _level_period_bounds(period, ref):
    """(start, end) of the period containing `ref`. Weeks run Monday-Saturday."""
    if period == "day":
        return ref, ref
    if period == "week":
        start = ref - timedelta(days=ref.weekday())
        return start, start + timedelta(days=5)
    start = ref.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def _level_judge(emp, period, start, end, daily=None):
    """(value, good, enough_days) for one period."""
    role = emp.staff_type
    days = daily if daily is not None else _level_daily(emp, start, end)
    days = {d: v for d, v in days.items() if start <= d <= end}
    if len(days) < LEVEL_MIN_DAYS[period]:
        return 0.0, False, False
    value = round(sum(v for v, _ in days.values()) / len(days), 1)
    if role in LEVEL_SCORED:
        entries = [e for _, e in days.values() if e is not None]
        st = build_analytics(entries, role, emp_id=emp.id) or {}
        good = float(st.get("eff_score") or 0) >= LEVEL_GOOD_SCORE
    else:
        good = True          # no score for this role: beating the record is the test
    return value, good, True


def _level_backfill(emp, period, before):
    """Set up a record from history without levelling anyone up, so switching
    the system on never floods staff with notifications for old days."""
    first = (db.session.query(db.func.min(KPIEntry.entry_date)).filter(KPIEntry.emp_id == emp.id).scalar()
             if emp.staff_type in LEVEL_SCORED else
             db.session.query(db.func.min(DeliveryBillerNote.entry_date)).filter(
                 DeliveryBillerNote.emp_id == emp.id).scalar())
    rec = PersonalRecord(emp_id=emp.id, period=period, best_value=0.0, level=0,
                         last_checked=before - timedelta(days=1))
    if not first or first >= before:
        return rec
    daily = _level_daily(emp, first, before - timedelta(days=1))
    # Every complete period before `before`, with its average -- cheap. Only
    # then score the candidates, highest first, stopping at the first that was
    # good: scoring every day of a long history would be slow.
    periods, ref = {}, first
    while ref < before:
        a, b = _level_period_bounds(period, ref)
        if b < before and (a, b) not in periods:
            vals = [v for d, (v, _) in daily.items() if a <= d <= b]
            if len(vals) >= LEVEL_MIN_DAYS[period]:
                periods[(a, b)] = round(sum(vals) / len(vals), 1)
        # Step to the next period. Weeks end on Saturday, but the Sunday after
        # still belongs to that same week, so step from the week's Monday.
        ref = (a + timedelta(days=7)) if period == "week" else (b + timedelta(days=1))
    for (a, b), v in sorted(periods.items(), key=lambda kv: kv[1], reverse=True):
        _, good, enough = _level_judge(emp, period, a, b, daily)
        if enough and good:
            rec.best_value, rec.best_start, rec.best_end = v, a, b
            break
    return rec


def _level_history_count(emp, period, before):
    """How many earlier periods with enough working days exist before `before`."""
    role = emp.staff_type
    q = (db.session.query(KPIEntry.entry_date).filter(KPIEntry.emp_id == emp.id, KPIEntry.entry_date < before)
         if role in LEVEL_SCORED else
         db.session.query(DeliveryBillerNote.entry_date).filter(DeliveryBillerNote.emp_id == emp.id,
                                                               DeliveryBillerNote.entry_date < before))
    dates = sorted({r[0] for r in q.all()})
    if period == "day":
        return len(dates)
    buckets = {}
    for d in dates:
        buckets.setdefault(_level_period_bounds(period, d), 0)
        buckets[_level_period_bounds(period, d)] += 1
    return sum(1 for k, n in buckets.items() if n >= LEVEL_MIN_DAYS[period] and k[1] < before)


def _level_notify(emp, ev, target):
    first = (emp.name or "").split(" ")[0] or "there"
    unit = ev.unit
    if ev.period == "day":
        title = "Level up! New best day"
        what = "%s %s on %s" % (_fmt_num(ev.value), unit, ev.start.strftime("%d %b"))
    elif ev.period == "week":
        title = "Level up! Best week ever"
        what = "%s %s a day in the week of %s" % (_fmt_num(ev.value), unit, ev.start.strftime("%d %b"))
    else:
        title = "Level up! Best month ever"
        what = "%s %s a day in %s" % (_fmt_num(ev.value), unit, ev.start.strftime("%B"))
    beat = ("beat your best of %s" % _fmt_num(ev.previous)) if ev.previous > 0 else "set your first record"
    tline = (" Your new target is %s %s." % (_fmt_num(target), unit if emp.staff_type in LEVEL_SCORED else "a day")
             if target else "")
    body = "Congratulations, %s! %s %s.%s You are now level %d." % (first, what, beat, tline, ev.level)
    notify_employee(emp.id, title, body, link="/dashboard", sms=False)
    return title, body


def _fmt_num(v):
    return ("%d" % v) if float(v).is_integer() else ("%.1f" % v)


def evaluate_levels(emp, day=None, today=None, notify=True):
    """Judge the given day (if any) plus the last completed week and month.
    Returns the level-up events created. Never raises: a failure here must
    not stop an entry from being saved."""
    events = []
    try:
        if not emp or emp.is_admin or emp.staff_type not in LEVEL_UNIT:
            return events
        today = today or date.today()
        todo = []
        if day:
            todo.append(("day", day, day))
        # last COMPLETED week (Mon-Sat ending before today) and month
        sat = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
        todo.append(("week", sat - timedelta(days=5), sat))
        m_end = today.replace(day=1) - timedelta(days=1)
        todo.append(("month", m_end.replace(day=1), m_end))
        unit = LEVEL_UNIT[emp.staff_type]
        for period, start, end in todo:
            rec = PersonalRecord.query.filter_by(emp_id=emp.id, period=period).first()
            if rec is None:
                rec = _level_backfill(emp, period, start)
                db.session.add(rec)
                db.session.flush()
            if rec.last_checked and end <= rec.last_checked and period != "day":
                continue
            if period == "day" and rec.last_checked and rec.last_checked == end:
                continue
            value, good, enough = _level_judge(emp, period, start, end)
            if period != "day" or (rec.last_checked is None or end > rec.last_checked):
                rec.last_checked = max(end, rec.last_checked) if rec.last_checked else end
            if not (enough and good):
                continue
            if _level_history_count(emp, period, start) < LEVEL_MIN_HISTORY[period]:
                continue
            if value <= (rec.best_value or 0) + 0.04:
                continue
            prev = rec.best_value or 0.0
            rec.best_value, rec.best_start, rec.best_end = value, start, end
            rec.level = (rec.level or 0) + 1
            rec.updated_at = datetime.utcnow()
            target = None
            if period == "day" and emp.staff_type in LEVEL_SCORED:
                # The best day becomes the target -- and a target only goes up.
                cur = emp.custom_hourly_target
                target = value if not cur else max(float(cur), value)
                emp.custom_hourly_target = target
            elif period == "day":
                target = value
            ev = LevelEvent(emp_id=emp.id, period=period, value=value, previous=prev,
                            start=start, end=end, level=rec.level, unit=unit)
            db.session.add(ev)
            events.append((ev, target))
        db.session.commit()
        invalidate_request_memos()
        out = []
        for ev, target in events:
            out.append((ev, _level_notify(emp, ev, target) if notify else None))
        return out
    except Exception as exc:
        db.session.rollback()
        logger.error("evaluate_levels(%s): %s", getattr(emp, "id", "?"), exc, exc_info=True)
        return []


def level_summary(emp):
    """What the staff member and admin see: level, records, target, history."""
    try:
        if not emp or emp.staff_type not in LEVEL_UNIT:
            return None
        recs = {r.period: r for r in PersonalRecord.query.filter_by(emp_id=emp.id).all()}
        events = (LevelEvent.query.filter_by(emp_id=emp.id)
                  .order_by(LevelEvent.created_at.desc()).limit(8).all())
        return {
            "level": sum((r.level or 0) for r in recs.values()),
            "unit": LEVEL_UNIT[emp.staff_type],
            "scored": emp.staff_type in LEVEL_SCORED,
            "target": emp.custom_hourly_target if emp.staff_type in LEVEL_SCORED else
                      (recs["day"].best_value if recs.get("day") and recs["day"].best_value else None),
            "day": recs.get("day"), "week": recs.get("week"), "month": recs.get("month"),
            "events": events,
            "fmt": _fmt_num,
        }
    except Exception as exc:
        logger.error("level_summary: %s", exc)
        return None


def level_status_all():
    """One row per staff member: does the level-up system apply to them yet,
    and if not, why. For the owner's review on the admin page.

    Statuses: "active" (records set, levels counting), "starting" (not enough
    working days logged yet), "waiting" (enough days, judged at the next save
    or the nightly check), "not_measured" (role has no output figure), plus a
    note when nothing was entered in the last 14 days.
    """
    from sqlalchemy import func
    rows = []
    try:
        # is_admin can be NULL on older live rows (not False), so a plain
        # "is_admin = false" filter matched nobody there. Filter in Python.
        staff = sorted((e for e in Employee.query.all() if not e.is_admin),
                       key=lambda e: ((e.staff_type or ""), (e.name or "").lower()))
        kpi = dict(db.session.query(KPIEntry.emp_id, func.count(func.distinct(KPIEntry.entry_date)))
                   .group_by(KPIEntry.emp_id).all())
        kpi_last = dict(db.session.query(KPIEntry.emp_id, func.max(KPIEntry.entry_date))
                        .group_by(KPIEntry.emp_id).all())
        notes = dict(db.session.query(DeliveryBillerNote.emp_id, func.count(func.distinct(DeliveryBillerNote.entry_date)))
                     .group_by(DeliveryBillerNote.emp_id).all())
        notes_last = dict(db.session.query(DeliveryBillerNote.emp_id, func.max(DeliveryBillerNote.entry_date))
                          .group_by(DeliveryBillerNote.emp_id).all())
        recs = {}
        for r in PersonalRecord.query.all():
            recs.setdefault(r.emp_id, {})[r.period] = r
        today = date.today()
        need = LEVEL_MIN_HISTORY["day"] + 1      # history days plus the day being judged
        for e in staff:
            role = e.staff_type
            scored = role in LEVEL_SCORED
            days = (kpi if scored else notes).get(e.id, 0) if role in LEVEL_UNIT else 0
            last = (kpi_last if scored else notes_last).get(e.id) if role in LEVEL_UNIT else None
            r = recs.get(e.id, {})
            row = {"emp": e, "role": role, "days": days, "last": last,
                   "unit": LEVEL_UNIT.get(role, ""),
                   "level": sum((x.level or 0) for x in r.values()),
                   "day": r.get("day"), "week": r.get("week"), "month": r.get("month"),
                   "target": (e.custom_hourly_target if scored else
                              (r["day"].best_value if r.get("day") and r["day"].best_value else None))}
            if role not in LEVEL_UNIT:
                row["status"], row["why"] = "not_measured", "No output figure for this role yet"
            elif days < need:
                row["status"], row["why"] = "starting", "%d of %d working days logged" % (days, need)
            elif not r:
                row["status"], row["why"] = "waiting", "Judged at the next entry or the nightly check"
            else:
                row["status"], row["why"] = "active", ""
            if row["status"] in ("active", "waiting") and (not last or (today - last).days > 14):
                row["why"] = "No entries in the last 14 days"
            elif row["status"] == "active" and last and r.get("day"):
                # Why the latest day did or did not level them up, in plain words.
                try:
                    v, good, enough = _level_judge(e, "day", last, last)
                    best = r["day"].best_value or 0
                    row["latest"] = v
                    bits = ["Latest day (%s): %s %s" % (last.strftime("%d %b"), _fmt_num(v), row["unit"])]
                    if v > best and good:
                        bits.append("a new best")
                    elif v > best:
                        bits.append("beat the best %s, but the day scored under %d" % (_fmt_num(best), LEVEL_GOOD_SCORE))
                    else:
                        bits.append("below the best of %s" % _fmt_num(best))
                    row["why"] = " · ".join(bits)
                except Exception as _exc:
                    logger.warning("level_status_all latest(%s): %s", e.id, _exc)
            rows.append(row)
    except Exception as exc:
        logger.error("level_status_all: %s", exc)
    return rows


def run_level_checks():
    """Daily: judge every staff member's last completed week and month."""
    with app.app_context():
        # Not "is_admin = false": older live rows hold NULL there, and that
        # filter silently skipped them, so weeks and months were never judged.
        for emp in Employee.query.filter(Employee.staff_type.in_(tuple(LEVEL_UNIT))).all():
            if not emp.is_admin:
                evaluate_levels(emp)


def multitask_days_for(emp_id, days=30):
    """Extra-role days in the last `days` days, one row per day, by the same
    rule the score uses (10 per role, capped at 30). For the explanation on
    the staff profile -- including people with no KPI score, like supervisors,
    whose extra roles still deserve to be shown and explained."""
    since = date.today() - timedelta(days=days)
    by_day = {}
    try:
        for m in MultitaskEntry.query.filter(MultitaskEntry.emp_id == emp_id,
                                             MultitaskEntry.entry_date >= since).all():
            by_day.setdefault(m.entry_date, set()).add(m.secondary_type)
    except Exception as exc:
        logger.error("multitask_days_for: %s", exc)
    return [{"date": d, "roles": sorted(rs), "raw": 10 * len(rs), "points": min(10 * len(rs), 30)}
            for d, rs in sorted(by_day.items(), reverse=True)]


def checker_month_bests(emp_id, today=None):
    """A checker's best day this month and last month, with the day-wise list.

    Bills = normal + urgent bills checked that day; items = normal + urgent
    items. The message tells the checker where they stand against their own
    record -- the one comparison that is always fair.
    """
    today = today or date.today()
    this_start = today.replace(day=1)
    prev_start = (this_start - timedelta(days=1)).replace(day=1)
    # Checking done as a primary role (KPI rows) and as a second role
    # (multitask rows) both count; one day's total is the two added together.
    class _Day:
        __slots__ = ("entry_date", "b", "i")
        def __init__(self, d): self.entry_date, self.b, self.i = d, 0, 0
    per = {}
    try:
        emp = db.session.get(Employee, emp_id)
        if emp and emp.staff_type == "checker":
            for e in KPIEntry.query.filter(KPIEntry.emp_id == emp_id,
                                           KPIEntry.entry_date >= prev_start,
                                           KPIEntry.entry_date <= today).all():
                r = per.setdefault(e.entry_date, _Day(e.entry_date))
                r.b += int(e.sales_bills_open or 0) + int(e.cs_sales_open or 0)
                r.i += int(e.checked or 0) + int(e.errors_found or 0)
        for m in MultitaskEntry.query.filter(MultitaskEntry.emp_id == emp_id,
                                             MultitaskEntry.secondary_type == "checker",
                                             MultitaskEntry.entry_date >= prev_start,
                                             MultitaskEntry.entry_date <= today).all():
            r = per.setdefault(m.entry_date, _Day(m.entry_date))
            r.b += int(m.sales_bills_open or 0) + int(m.cs_sales_open or 0)
            r.i += int(m.checked or 0) + int(m.errors_found or 0)
    except Exception as exc:
        logger.error("checker_month_bests: %s", exc)
    rows = sorted(per.values(), key=lambda r: r.entry_date)

    def bills(e): return e.b
    def items(e): return e.i

    def best(rs, fn):
        rs = [r for r in rs if fn(r) > 0]
        if not rs:
            return None
        top = max(rs, key=lambda r: (fn(r), r.entry_date))
        return {"value": fn(top), "date": top.entry_date}

    cur = [r for r in rows if r.entry_date >= this_start]
    prev = [r for r in rows if r.entry_date < this_start]
    before_today = [r for r in cur if r.entry_date < today]
    today_row = next((r for r in cur if r.entry_date == today), None)
    out = {
        "this": {"label": this_start.strftime("%B"), "bills": best(cur, bills), "items": best(cur, items)},
        "prev": {"label": prev_start.strftime("%B"), "bills": best(prev, bills), "items": best(prev, items)},
        "days": [{"date": r.entry_date, "bills": bills(r), "items": items(r)} for r in reversed(cur)],
        "message": "", "record": False,
    }
    if today_row:
        b0, i0 = best(before_today, bills), best(before_today, items)
        wins = []
        if bills(today_row) > (b0["value"] if b0 else 0) and bills(today_row) > 0:
            wins.append("%d bills" % bills(today_row))
        if items(today_row) > (i0["value"] if i0 else 0) and items(today_row) > 0:
            wins.append("%d items" % items(today_row))
        if wins and before_today:
            out["record"] = True
            out["message"] = "New record! Today's %s is your best day this month." % " and ".join(wins)
    if not out["message"]:
        tb, ti = out["this"]["bills"], out["this"]["items"]
        # Bills and items are recorded separately; either can be missing on a
        # day (a second-role checker may log bills but no item count).
        parts = []
        if tb:
            parts.append("%d bills on %s" % (tb["value"], tb["date"].strftime("%d %b")))
        if ti:
            parts.append("%d items on %s" % (ti["value"], ti["date"].strftime("%d %b")))
        if parts:
            out["message"] = "Your best this month: %s. Beat it today!" % " and ".join(parts)
        elif out["prev"]["bills"]:
            pb = out["prev"]["bills"]
            out["message"] = ("Last month's best was %d bills on %s. Set this month's first record."
                              % (pb["value"], pb["date"].strftime("%d %b")))
        else:
            out["message"] = "No checking recorded yet this month."
    return out


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
        complaints = req_memo("open_complaints", 0,
                              lambda: Complaint.query.filter_by(is_resolved=False).all())
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

# ─── Bill-based picking: the rules ───────────────────────────────────────────
# Timeline of one grab of n items (s = 30 s per item, 20 s on an urgent bill):
#   normal time   0 .. n*s            pick or report; finishing here earns most
#   find time     + 62.5 s            still pick or report; no points cut
#   report time   + 62.5 s            picking locked: only report missing items
#   late          after that          late penalty and the assigner is alerted
class PickError(Exception):
    """A picking action that cannot be done; the message is shown to the user."""


IST = timedelta(hours=5, minutes=30)


def ist12(dt):
    """A UTC time as Indian time in 12-hour form, e.g. 2:05 PM."""
    if not dt:
        return ""
    return (dt + IST).strftime("%I:%M %p").lstrip("0")


def fy_prefix(d=None):
    """Bill number prefix for the financial year (April to March): IP2627/."""
    d = d or (datetime.utcnow() + IST).date()
    a = (d.year if d.month >= 4 else d.year - 1) % 100
    return "IP%02d%02d/" % (a, (a + 1) % 100)


def pick_seconds(bill):
    return PICK_URGENT_SECONDS if bill is not None and bill.is_urgent else PICK_SECONDS_PER_ITEM


def pick_windows(c):
    """(normal_end, find_end, report_end) for a grab."""
    half = timedelta(seconds=PICK_EXTRA_SECONDS / 2.0)
    return c.due_at, c.due_at + half, c.due_at + 2 * half


def pick_phase(c, now):
    n, f, r = pick_windows(c)
    return "normal" if now <= n else "find" if now <= f else "report" if now <= r else "late"


def pick_points(items, due, done, picked=None, missing=0):
    """Points for one finished grab. picked defaults to all items."""
    picked = items if picked is None else picked
    half = timedelta(seconds=PICK_EXTRA_SECONDS / 2.0)
    if done <= due:                       # normal time: more points; missing reported now counts in full
        return round(PICK_ONTIME_RATE * (picked + missing), 1)
    if done <= due + 2 * half:            # find or report time: no cut
        return float(picked)
    late_min = math.ceil((done - due - 2 * half).total_seconds() / 60.0)
    return -float(min(PICK_LATE_CAP, max(1, late_min)))


def pick_bill_progress(bill):
    """Where a bill stands, and who is on it."""
    claims = PickClaim.query.filter(PickClaim.bill_id == bill.id, PickClaim.status != "cancelled").all()
    miss = PickMissing.query.filter_by(bill_id=bill.id).all()
    done = sum((c.picked if c.picked is not None else c.items) for c in claims if c.status == "done")
    active = sum(c.items for c in claims if c.status == "active")
    m_open = sum(m.qty for m in miss if m.status == "open")
    m_gone = sum(m.qty for m in miss if m.status == "unavailable")
    by = {}
    for c in claims:
        r = by.setdefault(c.picker_id, {"items": 0, "active": 0, "late": False})
        if c.status == "done":
            r["items"] += c.picked if c.picked is not None else c.items
            r["late"] = r["late"] or c.outcome == "late"
        else:
            r["active"] += c.items
    return {"done": done, "active": active, "missing": m_open, "unavailable": m_gone,
            "left": max(int(bill.items) - done - active - m_open - m_gone, 0), "by": by,
            "pickers": sorted(by), "missing_rows": [m for m in miss if m.status == "open"]}


def active_claim_for(picker_id):
    return PickClaim.query.filter_by(picker_id=picker_id, status="active").first()


def free_pickers():
    """Pickers with no grab in progress right now."""
    busy = {c.picker_id for c in PickClaim.query.filter_by(status="active").all()}
    return [e for e in Employee.query.filter(Employee.staff_type == "picker").order_by(Employee.name).all()
            if not e.is_admin and e.id not in busy]


def _assigners():
    return Employee.query.filter(Employee.staff_type == "pick_assigner").all()


def _pick_event(kind, picker_id, bill_id, minutes=None, detail=""):
    db.session.add(PickEvent(kind=kind, picker_id=picker_id, bill_id=bill_id, minutes=minutes,
                             detail=(detail or "")[:200], day=date.today()))


def add_pick_bill(bill_no, items, created_by):
    """bill_no is what the assigner types: up to 6 digits, U on the end for
    urgent. The financial-year prefix is added here; a number is never reused."""
    raw = (bill_no or "").strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,6})(U?)$", raw)
    if not m:
        raise PickError("Bill number: up to 6 digits, with U on the end if urgent (e.g. 4521 or 4521U).")
    try:
        items = int(items)
    except (TypeError, ValueError):
        items = 0
    if items < 1 or items > 500:
        raise PickError("Items must be between 1 and 500.")
    if int(m.group(1)) == 0:
        raise PickError("Bill number cannot be 0.")
    full = fy_prefix() + m.group(1)
    if PickBill.query.filter_by(bill_no=full).first():
        raise PickError("Bill %s was already entered. A bill number can be used only once." % full)
    bill = PickBill(bill_no=full, items=items, created_by=created_by, is_urgent=bool(m.group(2)))
    db.session.add(bill)
    db.session.commit()
    title = ("URGENT bill %s" if bill.is_urgent else "New bill %s") % full
    for p in free_pickers():
        notify_employee(p.id, title, "%d items to pick" % items, link="/dashboard", sms=False)
    return bill


def claim_items(picker_id, bill_id, n, now=None):
    """Picker types how many items they are taking. One grab at a time;
    urgent bills come first."""
    now = now or datetime.utcnow()
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise PickError("Type how many items you are taking.")
    db.session.query(Employee).filter_by(id=picker_id).with_for_update().first()
    if active_claim_for(picker_id):
        raise PickError("Finish your current items first.")
    bill = db.session.query(PickBill).filter_by(id=bill_id).with_for_update().first()
    if not bill or bill.status != "open":
        db.session.rollback()
        raise PickError("That bill is no longer open.")
    if not bill.is_urgent:
        for u in PickBill.query.filter_by(status="open", is_urgent=True).all():
            if pick_bill_progress(u)["left"] > 0:
                db.session.rollback()
                raise PickError("Urgent bill %s comes first." % u.bill_no)
    left = pick_bill_progress(bill)["left"]
    if n > left:
        db.session.rollback()
        raise PickError("Only %d item%s left on bill %s." % (left, "" if left == 1 else "s", bill.bill_no))
    c = PickClaim(bill_id=bill.id, picker_id=picker_id, items=n, started_at=now,
                  due_at=now + timedelta(seconds=n * pick_seconds(bill)))
    db.session.add(c)
    db.session.commit()
    return c


def _close_bill_if_done(bill, now):
    if not bill or bill.status != "open":
        return False
    pr = pick_bill_progress(bill)
    if pr["done"] + pr["unavailable"] < bill.items or pr["active"] or pr["missing"]:
        return False
    bill.status, bill.completed_at = "done", now
    if bill.is_urgent:                    # share the pool by items picked
        claims = PickClaim.query.filter_by(bill_id=bill.id, status="done").all()
        total = sum((c.picked or 0) for c in claims)
        for c in claims:
            c.bonus = round(PICK_URGENT_POOL * (c.picked or 0) / total, 1) if total else 0.0
    return True


def finish_claim(picker_id, claim_id, now=None, day=None, picked=None, missing=None):
    """Picker finishes a grab: all picked, or some picked and the rest
    reported missing ([(item name, qty), ...]). Returns (claim, bill_done)."""
    now = now or datetime.utcnow()
    c = db.session.get(PickClaim, claim_id)
    if not c or c.picker_id != picker_id or c.status != "active":
        raise PickError("Nothing to finish.")
    missing = [(str(n or "").strip()[:120], int(q or 0)) for n, q in (missing or [])]
    missing = [(n, q) for n, q in missing if q > 0]
    if any(not n for n, _ in missing):
        raise PickError("Write the name of each missing item.")
    m_qty = sum(q for _, q in missing)
    picked = c.items - m_qty if picked is None else int(picked)
    if picked < 0 or picked + m_qty != c.items:
        raise PickError("Picked plus missing must equal the %d items you took." % c.items)
    phase = pick_phase(c, now)
    if phase == "report" and not missing:
        raise PickError("Picking time is over. Report the items you could not find.")
    c.done_at, c.status, c.picked, c.missing = now, "done", picked, m_qty
    c.work_date = day or date.today()      # same day boundary as the rest of the app
    c.outcome = {"normal": "ontime", "find": "extra", "report": "report", "late": "late"}[phase]
    # Points for missing items are held until the assigner confirms them as
    # not available; otherwise reporting everything missing would pay for
    # nothing picked.
    c.points = pick_points(c.items, c.due_at, now, picked=picked, missing=0)
    bill = db.session.get(PickBill, c.bill_id)
    for name, q in missing:
        db.session.add(PickMissing(bill_id=c.bill_id, claim_id=c.id, picker_id=picker_id,
                                   item_name=name, qty=q, reported_at=now))
        _pick_event("missing", picker_id, c.bill_id, detail="%s x%d" % (name, q))
    if phase == "late":
        mins = round((now - pick_windows(c)[2]).total_seconds() / 60.0, 1)
        ev = PickEvent.query.filter_by(kind="late", picker_id=picker_id, bill_id=c.bill_id).order_by(PickEvent.id.desc()).first()
        if c.late_alerted and ev:
            ev.minutes, ev.detail = mins, "finished %g min late" % mins
        else:
            c.late_alerted = True
            _pick_event("late", picker_id, c.bill_id, minutes=mins, detail="finished %g min late" % mins)
    db.session.flush()
    completed = _close_bill_if_done(bill, now)
    db.session.commit()
    if missing:
        who = db.session.get(Employee, picker_id)
        for a in _assigners():
            notify_employee(a.id, "Missing items on bill %s" % (bill.bill_no if bill else "?"),
                            "%s could not find: %s" % (who.name if who else "A picker",
                                                       ", ".join("%s x%d" % mq for mq in missing)),
                            link="/dashboard", sms=False)
    sync_picker_day(picker_id, c.work_date)
    return c, completed


def handle_missing(missing_id, action, now=None):
    """Assigner deals with a missing-item report: put the items back on the
    bill for another picker (reassign), or mark them not available."""
    now = now or datetime.utcnow()
    m = db.session.get(PickMissing, missing_id)
    if not m or m.status != "open":
        raise PickError("That report has already been handled.")
    if action not in ("reassign", "unavailable"):
        raise PickError("Choose reassign or not available.")
    m.status, m.handled_at = ("reassigned" if action == "reassign" else "unavailable"), now
    if action == "unavailable" and m.claim_id:
        # Confirmed missing: the reporter is paid for it as if picked, at the
        # rate of when they reported it (normal time 1.5, extra time 1).
        rc = db.session.get(PickClaim, m.claim_id)
        if rc and rc.outcome in ("ontime", "extra", "report"):
            rc.points = round(float(rc.points or 0) + (PICK_ONTIME_RATE if rc.outcome == "ontime" else 1.0) * m.qty, 1)
    if action == "reassign":
        # the items go back on the bill as ordinary items to pick
        bill = db.session.get(PickBill, m.bill_id)
        if bill and bill.status == "done":
            bill.status, bill.completed_at = "open", None
    db.session.flush()
    _close_bill_if_done(db.session.get(PickBill, m.bill_id), now)
    db.session.commit()
    if action == "reassign":
        bill = db.session.get(PickBill, m.bill_id)
        for p in free_pickers():
            notify_employee(p.id, "Bill %s: %d item%s to pick again" % (bill.bill_no, m.qty, "" if m.qty == 1 else "s"),
                            m.item_name, link="/dashboard", sms=False)
    return m


def release_claim(claim_id):
    """Assigner frees a stuck grab; its items go back on the bill."""
    c = db.session.get(PickClaim, claim_id)
    if not c or c.status != "active":
        raise PickError("That grab is not in progress.")
    c.status = "cancelled"
    db.session.commit()


def cancel_pick_bill(bill_id):
    bill = db.session.get(PickBill, bill_id)
    if not bill or bill.status != "open":
        raise PickError("That bill is not open.")
    bill.status = "cancelled"
    for c in PickClaim.query.filter_by(bill_id=bill.id, status="active").all():
        c.status = "cancelled"
    db.session.commit()


def reschedule_bill(bill_id, when_utc):
    """Move an unfinished bill to a later time. Grabs in progress must finish first."""
    bill = db.session.get(PickBill, bill_id)
    if not bill or bill.status not in ("open", "scheduled"):
        raise PickError("Only an unfinished bill can be rescheduled.")
    if PickClaim.query.filter_by(bill_id=bill.id, status="active").first():
        raise PickError("Someone is picking this bill now. Wait until they finish.")
    if not when_utc or when_utc <= datetime.utcnow():
        raise PickError("Choose a time in the future.")
    bill.status, bill.scheduled_for = "scheduled", when_utc
    db.session.commit()
    return bill


def bring_back_bill(bill_id):
    bill = db.session.get(PickBill, bill_id)
    if not bill or bill.status != "scheduled":
        raise PickError("That bill is not rescheduled.")
    _reopen(bill)
    db.session.commit()
    return bill


def _reopen(bill):
    bill.status, bill.scheduled_for = "open", None
    for p in free_pickers():
        notify_employee(p.id, ("URGENT bill %s is back" if bill.is_urgent else "Bill %s is back") % bill.bill_no,
                        "%d items to pick" % pick_bill_progress(bill)["left"], link="/dashboard", sms=False)


def close_pick_day(closed_by, day=None):
    """End of the picking day: count the day's bills (not cancelled) and fix
    that number as the checkers' day total. Unfinished bills move to
    Rescheduled for 9:00 AM next day."""
    day = day or date.today()
    if PickDay.query.filter_by(day=day).first():
        raise PickError("Today is already closed.")
    start = datetime.combine(day, datetime.min.time())
    bills = PickBill.query.filter(PickBill.created_at >= start, PickBill.created_at < start + timedelta(days=1),
                                  PickBill.status != "cancelled").all()
    if PickClaim.query.filter_by(status="active").first():
        raise PickError("Pickers are still picking. Close the day when every grab is finished.")
    moved = 0
    next_morning = datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=9) - IST
    for b in PickBill.query.filter_by(status="open").all():
        b.status, b.scheduled_for = "scheduled", next_morning
        moved += 1
    pdz = PickDay(day=day, total_bills=len(bills), closed_by=closed_by)
    db.session.add(pdz)
    db.session.commit()
    return pdz, moved


def pick_day_total(day):
    """The checkers' fixed day total, if the Pick Assigner closed that day."""
    try:
        r = PickDay.query.filter_by(day=day).first()
        return r.total_bills if r else None
    except Exception:
        return None


def bill_timeline(bill_id):
    """Everything that happened on one bill, in order, for the detail view."""
    b = db.session.get(PickBill, bill_id)
    if not b:
        raise PickError("Bill not found.")
    names = {e.id: e.name for e in Employee.query.all()}
    rows = []
    for c in PickClaim.query.filter_by(bill_id=b.id).order_by(PickClaim.started_at).all():
        n, f, r = pick_windows(c)
        rows.append({"picker": names.get(c.picker_id, ""), "items": c.items,
                     "picked": c.picked if c.picked is not None else (c.items if c.status == "done" else None),
                     "missing": c.missing or 0, "took": ist12(c.started_at), "due": ist12(n), "done": ist12(c.done_at),
                     "status": c.status, "outcome": c.outcome or "", "points": float(c.points or 0) + float(c.bonus or 0),
                     "minutes": round(((c.done_at or datetime.utcnow()) - c.started_at).total_seconds() / 60.0, 1),
                     "late_min": round((c.done_at - r).total_seconds() / 60.0, 1) if c.done_at and c.done_at > r else None})
    miss = [{"item": m.item_name, "qty": m.qty, "by": names.get(m.picker_id, ""), "at": ist12(m.reported_at),
             "status": m.status} for m in PickMissing.query.filter_by(bill_id=b.id).order_by(PickMissing.reported_at).all()]
    return {"bill_no": b.bill_no, "items": b.items, "urgent": bool(b.is_urgent), "status": b.status,
            "added": ist12(b.created_at), "completed": ist12(b.completed_at),
            "minutes": round((b.completed_at - b.created_at).total_seconds() / 60.0, 1) if b.completed_at else None,
            "grabs": rows, "missing": miss}


def pick_board(day=None):
    """Today's leaderboard: bills first, then items, then on time, then points."""
    day = day or date.today()
    rows = []
    for e in Employee.query.filter(Employee.staff_type == "picker").all():
        if e.is_admin:
            continue
        st = picker_pick_stats(e.id, [day])
        rows.append({"id": e.id, "name": e.name, "bills": st["bills"], "items": st["items"], "grabs": st["grabs"],
                     "on_time_pct": round(100.0 * st["on_time"] / st["grabs"], 1) if st["grabs"] else 0.0,
                     "points": st["points"], "missing": st["missing"], "rate": st["daily_rate"].get(day) or 0})
    rows.sort(key=lambda r: (-r["bills"], -r["items"], -r["on_time_pct"], -r["points"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def picker_bill_list(emp_id, day=None):
    """A picker's day, bill by bill: Bill IP2627/4521 - 5 items."""
    day = day or date.today()
    by = {}
    for c in PickClaim.query.filter_by(picker_id=emp_id, status="done", work_date=day).order_by(PickClaim.done_at).all():
        b = db.session.get(PickBill, c.bill_id)
        r = by.setdefault(c.bill_id, {"bill_id": c.bill_id, "bill_no": b.bill_no if b else "", "urgent": bool(b and b.is_urgent),
                                      "items": 0, "missing": 0, "points": 0.0, "grabs": 0, "late": False, "last": ""})
        r["items"] += c.picked if c.picked is not None else c.items
        r["missing"] += c.missing or 0
        r["points"] = round(r["points"] + float(c.points or 0) + float(c.bonus or 0), 1)
        r["grabs"] += 1
        r["late"] = r["late"] or c.outcome == "late"
        r["last"] = ist12(c.done_at)
    return list(by.values())


def picker_pick_stats(emp_id, dates):
    """Finished grabs on these days. The basis of the picker score."""
    dates = [d for d in dates if d]
    out = {"items": 0, "grabs": 0, "on_time": 0, "points": 0.0, "seconds": 0.0, "bills": 0,
           "missing": 0, "daily_rate": {}, "daily_items": {}}
    if emp_id is None or not dates:
        return out
    claims = PickClaim.query.filter(PickClaim.picker_id == emp_id, PickClaim.status == "done",
                                    PickClaim.work_date.in_(dates)).all()
    per_day, bills = {}, set()
    for c in claims:
        got = c.picked if c.picked is not None else c.items
        # A grab tapped Done in a few seconds would give absurd speeds; count
        # at least 5 s per item.
        secs = max((c.done_at - c.started_at).total_seconds() if c.done_at and c.started_at else 0.0, 5.0 * got)
        out["items"] += got
        out["grabs"] += 1
        out["missing"] += c.missing or 0
        out["on_time"] += 1 if (c.outcome in ("ontime", "extra", "report") if c.outcome else c.done_at <= c.due_at) else 0
        out["points"] += float(c.points or 0) + float(c.bonus or 0)
        out["seconds"] += secs
        bills.add(c.bill_id)
        d = per_day.setdefault(c.work_date, [0, 0.0])
        d[0] += got
        d[1] += secs
    out["points"] = round(out["points"], 1)
    out["bills"] = len(bills)
    for d, (it, secs) in per_day.items():
        out["daily_items"][d] = it
        out["daily_rate"][d] = round(it / (secs / 3600.0), 1)
    return out


def sync_picker_day(emp_id, d):
    """Keep one KPIEntry per picker per day in step with their finished grabs,
    so leaderboards, reports, history and level-ups keep working unchanged."""
    st = picker_pick_stats(emp_id, [d])
    e = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=d).first()
    if not e:
        if not st["grabs"]:
            return None
        e = KPIEntry(emp_id=emp_id, entry_date=d, total_time=9.0)
        db.session.add(e)
    e.picked = st["items"]
    e.missed = st["missing"]
    e.sales_bills_open = st["bills"]
    e.total_bills_received = st["bills"]
    e.total_time = 9.0
    db.session.commit()
    try:
        emp = db.session.get(Employee, emp_id)
        if emp:
            evaluate_levels(emp, day=d)
    except Exception as exc:
        logger.error("sync_picker_day levels: %s", exc)
    return e


def alert_late_picks(now=None):
    """Run on every screen refresh and once a minute:
      - a grab past its report time is late: the assigner is told once
      - a free picker who has not taken a waiting bill within 2 minutes gets
        a warning (no points cut for now) and the assigner is told
    Returns how many new late grabs were found."""
    now = now or datetime.utcnow()
    try:
        assigners = _assigners()
        for b in PickBill.query.filter(PickBill.status == "scheduled", PickBill.scheduled_for <= now).all():
            _reopen(b)
        db.session.commit()
        half = timedelta(seconds=PICK_EXTRA_SECONDS / 2.0)
        late = [c for c in PickClaim.query.filter(PickClaim.status == "active",
                                                  PickClaim.due_at < now - 2 * half).all()
                if not c.late_alerted]
        for c in late:
            c.late_alerted = True
            _pick_event("late", c.picker_id, c.bill_id, minutes=0.0, detail="still not finished")
        # free pickers who leave a waiting bill untouched
        idle_new = []
        waiting = [b for b in PickBill.query.filter_by(status="open").all() if pick_bill_progress(b)["left"] > 0]
        if waiting:
            warned = {(e.picker_id, e.bill_id) for e in PickEvent.query.filter_by(kind="idle").all()}
            last_warn = dict(db.session.query(PickEvent.picker_id, db.func.max(PickEvent.at))
                             .filter(PickEvent.kind == "idle").group_by(PickEvent.picker_id).all())
            on_duty = now - timedelta(minutes=5)
            for p in free_pickers():
                if not p.last_seen_at or p.last_seen_at < on_duty:
                    continue          # not at work / not on the picking screen
                if last_warn.get(p.id) and last_warn[p.id] >= (max(
                        (db.session.query(db.func.max(PickClaim.done_at)).filter(PickClaim.picker_id == p.id).scalar()
                         or datetime.min), min(b.created_at for b in waiting))):
                    continue          # already warned for this idle stretch
                last = (db.session.query(db.func.max(PickClaim.done_at))
                        .filter(PickClaim.picker_id == p.id).scalar())
                for b in waiting:
                    since = max(b.created_at, last) if last else b.created_at
                    if (now - since).total_seconds() >= PICK_IDLE_SECONDS and (p.id, b.id) not in warned:
                        mins = round((now - since).total_seconds() / 60.0, 1)
                        _pick_event("idle", p.id, b.id, minutes=mins, detail="did not take a waiting bill")
                        idle_new.append((p, b, mins))
                        break
        if not late and not idle_new:
            return 0
        db.session.commit()
        for c in late:
            who = db.session.get(Employee, c.picker_id)
            bill = db.session.get(PickBill, c.bill_id)
            title = "%s is late on bill %s" % (who.name if who else "A picker", bill.bill_no if bill else "?")
            body = "%d item%s, time ran out at %s." % (c.items, "" if c.items == 1 else "s", ist12(pick_windows(c)[2]))
            for a in assigners:
                notify_employee(a.id, title, body, link="/dashboard", sms=False)
        for p, b, mins in idle_new:
            notify_employee(p.id, "Please take the waiting bill",
                            "Bill %s has been waiting %g min while you were free. Leaving bills waiting "
                            "will lead to point deductions from your day's points." % (b.bill_no, mins),
                            link="/dashboard", sms=False)
            for a in assigners:
                notify_employee(a.id, "%s left bill %s waiting" % (p.name, b.bill_no),
                                "Free for %g min without taking it." % mins, link="/dashboard", sms=False)
        return len(late)
    except Exception as exc:
        db.session.rollback()
        logger.error("alert_late_picks: %s", exc)
        return 0


def run_late_pick_alerts():
    with app.app_context():
        alert_late_picks()


def picking_state(emp, staff_type):
    """Everything the picking screens show, as plain JSON-able data."""
    now = datetime.utcnow()
    today = date.today()
    day_start = datetime.combine(today, datetime.min.time())
    alert_late_picks(now)
    bills = (PickBill.query.filter(or_(PickBill.status == "open", PickBill.created_at >= day_start))
             .order_by(PickBill.is_urgent.desc(), PickBill.created_at.desc()).limit(80).all())
    names = {e.id: e.name for e in Employee.query.all()}
    active_now = {c.picker_id: c for c in PickClaim.query.filter_by(status="active").all()}

    def bill_row(b):
        pr = pick_bill_progress(b)
        mins = round((b.completed_at - b.created_at).total_seconds() / 60.0, 1) if b.completed_at and b.created_at else None
        taken = []
        for pid, r in pr["by"].items():
            ac = active_now.get(pid)
            late_now = bool(ac and ac.bill_id == b.id and pick_phase(ac, now) == "late")
            taken.append({"name": names.get(pid, "#%d" % pid), "items": r["items"] + r["active"],
                          "state": "late" if (r["late"] or late_now) else ("picking" if r["active"] else "done")})
        return {"id": b.id, "bill_no": b.bill_no, "items": b.items, "status": b.status, "urgent": bool(b.is_urgent),
                "done": pr["done"], "active": pr["active"], "left": pr["left"], "missing": pr["missing"],
                "taken": taken, "pickers": [t["name"] for t in taken],
                "missing_rows": [{"id": m.id, "item": m.item_name, "qty": m.qty, "by": names.get(m.picker_id, ""),
                                  "at": ist12(m.reported_at)} for m in pr["missing_rows"]],
                "elapsed": int(((b.completed_at or now) - b.created_at).total_seconds()) if b.created_at else 0,
                "minutes": mins, "added": ist12(b.created_at)}

    out = {"board": pick_board(today), "me": emp.id if emp else None,
           "day_closed": pick_day_total(today),
           "scheduled": [{"id": b.id, "bill_no": b.bill_no, "items": b.items, "urgent": bool(b.is_urgent),
                          "left": pick_bill_progress(b)["left"], "when": ist12(b.scheduled_for),
                          "when_day": (b.scheduled_for + IST).strftime("%d %b") if b.scheduled_for else ""}
                         for b in PickBill.query.filter_by(status="scheduled").order_by(PickBill.scheduled_for).all()],
           "seconds_per_item": PICK_SECONDS_PER_ITEM, "urgent_seconds": PICK_URGENT_SECONDS,
           "extra_seconds": PICK_EXTRA_SECONDS, "late_cap": PICK_LATE_CAP, "prefix": fy_prefix(),
           "bills": [bill_row(b) for b in bills]}
    if staff_type == "picker":
        c = active_now.get(emp.id)
        if c:
            b = db.session.get(PickBill, c.bill_id)
            n, f, r = pick_windows(c)
            out["mine"] = {"id": c.id, "bill_no": b.bill_no if b else "", "items": c.items,
                           "urgent": bool(b and b.is_urgent),
                           "normal_left": int((n - now).total_seconds()), "find_left": int((f - now).total_seconds()),
                           "report_left": int((r - now).total_seconds()),
                           "seconds_total": c.items * pick_seconds(b)}
        out["my_bills"] = picker_bill_list(emp.id, today)
        st = picker_pick_stats(emp.id, [today])
        out["today"] = {"bills": st["bills"], "items": st["items"], "grabs": st["grabs"], "points": st["points"],
                        "on_time_pct": round(100.0 * st["on_time"] / st["grabs"], 1) if st["grabs"] else None,
                        "rate": st["daily_rate"].get(today), "missing": st["missing"]}
    else:
        rows = []
        for e in Employee.query.filter(Employee.staff_type == "picker").order_by(Employee.name).all():
            if e.is_admin:
                continue
            c = active_now.get(e.id)
            r = {"id": e.id, "name": e.name, "free": c is None}
            if c:
                b = db.session.get(PickBill, c.bill_id)
                r.update({"claim_id": c.id, "bill_no": b.bill_no if b else "", "items": c.items,
                          "seconds_left": int((c.due_at - now).total_seconds()), "phase": pick_phase(c, now)})
                if pick_phase(c, now) == "late":
                    r["late_by"] = int((now - pick_windows(c)[2]).total_seconds())
            st = picker_pick_stats(e.id, [today])
            r.update({"today_bills": st["bills"], "today_items": st["items"], "today_points": st["points"]})
            rows.append(r)
        out["pickers"] = rows
        evs = PickEvent.query.filter_by(day=today).order_by(PickEvent.at.desc()).limit(100).all()
        bill_no = {b.id: b.bill_no for b in PickBill.query.filter(PickBill.id.in_({e.bill_id for e in evs if e.bill_id})).all()} if evs else {}
        out["events"] = [{"kind": e.kind, "who": names.get(e.picker_id, ""), "bill": bill_no.get(e.bill_id, ""),
                          "at": ist12(e.at), "minutes": e.minutes, "detail": e.detail or ""} for e in evs]
    return out


def build_analytics(entries: List[KPIEntry], staff_type: str = "picker", emp_id: int = None) -> Optional[Dict[str, Any]]:
    try:
        if not entries: return None

        # Only three roles have a KPI point system. Anything else used to fall
        # through to the checker formula below, which scored a supervisor's or a
        # packer's numbers with maths built for a different job -- producing a
        # meaningless score and, worse, a false RE-TRAINING grade.
        #
        # Resolve what the figures should actually be scored as: the employee's
        # own role when it is a scored one, otherwise the secondary role they
        # multitask under (a supervisor who also picks enters picker numbers),
        # and if neither applies the entries are reported but not graded.
        SCORED_ROLES = ("picker", "checker", "purchaser")
        primary_role = staff_type
        is_scored = True
        if staff_type not in SCORED_ROLES:
            _sec = ""
            try:
                if emp_id is not None:
                    _e = db.session.get(Employee, emp_id)
                    _sec = (_e.secondary_staff_type or "") if _e else ""
            except Exception:
                _sec = ""
            if _sec in SCORED_ROLES:
                staff_type = _sec
            else:
                is_scored = False
                staff_type = "picker"   # neutral basis; the result is flagged below

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
        # Checker: the day's total bills is a shared figure, so it is taken once
        # per day (the max of what the checkers on duty entered) rather than
        # summed across their rows -- three checkers each entering "100" for the
        # same day means 100 bills, not 300.
        if staff_type == "checker":
            _team_days = checker_team_day_totals({e.entry_date for e in entries})
            tbr  = sum(r["total"] for r in _team_days.values())
            team_checked_bills = sum(r["checked"] for r in _team_days.values())
            team_pending_bills = sum(r["pending"] for r in _team_days.values())
            team_urgent_bills  = sum(r["urgent"] for r in _team_days.values())
            team_checker_count = len({eid for r in _team_days.values() for eid in r["by_emp"]})
            team_per_person_days = sum(1 for r in _team_days.values() if r["mode"] == "per_person")
            team_conflict_days = sum(1 for r in _team_days.values() if r["mode"] == "conflict")
        else:
            _team_days = {}
            tbr = team_checked_bills = team_pending_bills = 0
            team_urgent_bills = team_checker_count = 0
            team_per_person_days = team_conflict_days = 0
        # Kept only so an old hand-typed figure can still be shown beside the
        # derived one while the team gets used to the change. It no longer scores.
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

        # Checker: clearance is how much of the day's pile the TEAM got through.
        #   pending     = day's total bills - every checker's checked bills
        #   clearance % = team checked / day's total
        # It is a shared figure on purpose: bills left on the counter are the
        # checking team's result, not one person's, and scoring it per-person
        # rewarded whoever happened to enter the smallest pending guess.
        tsb_checked_total = tsb_normal + tsb_urgent if staff_type == "checker" else 0
        pending_bills  = team_pending_bills
        total_for_clearance = tbr
        clearance_rate = (round(min(safe_div(team_checked_bills, total_for_clearance) * 100, 100.0), 1)
                          if total_for_clearance > 0 else 100.0)
        # This checker's share of what the team cleared — the part of the team
        # result that is actually theirs.
        team_share_pct = (round(safe_div(tsb_checked_total, team_checked_bills) * 100, 1)
                          if team_checked_bills > 0 else 0.0)

        # ── Urgent work counts for more than normal work ───────────────────
        # Volume used to be a plain head-count, so an urgent bill and a normal
        # bill earned the same. Urgent bills interrupt, cannot be batched and
        # have someone waiting, so they are credited at URGENT_BILL_WEIGHT.
        urgent_weight     = URGENT_BILL_WEIGHT
        weighted_bills    = round(tsb_normal + tsb_urgent * urgent_weight, 1)
        weighted_items    = round(tck_normal + tck_urgent * urgent_weight, 1)
        # The extra credit urgent work earned, in plain "equivalent items".
        urgent_item_bonus = round(tck_urgent * (urgent_weight - 1.0), 1)
        urgent_bill_bonus = round(tsb_urgent * (urgent_weight - 1.0), 1)
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
        # What the speed component is actually scored on: urgent items counted
        # at their weight. Shown beside the raw figure so the difference between
        # "items an hour" and "what it was worth" is visible.
        check_speed_weighted = round(safe_div(weighted_items, ts_total), 1)
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
        # Hourly targets. Defaults come from env so they can be tuned to what
        # this warehouse actually achieves without a code change; a per-employee
        # custom_hourly_target still overrides them.
        PICKER_TARGET_HR  = DEFAULT_PICKER_TARGET_HR
        CHECKER_TARGET_HR = DEFAULT_CHECKER_TARGET_HR
        # Check for per-employee override
        PURCHASER_TARGET_HR = 60.0
        _emp_for_target = None
        try:
            if emp_id is not None:
                _emp_for_target = db.session.get(Employee, emp_id)
                if _emp_for_target and _emp_for_target.custom_hourly_target:
                    if staff_type == "picker":
                        PICKER_TARGET_HR = float(_emp_for_target.custom_hourly_target)
                    elif staff_type == "checker":
                        CHECKER_TARGET_HR = float(_emp_for_target.custom_hourly_target)
                    elif staff_type == "purchaser":
                        PURCHASER_TARGET_HR = float(_emp_for_target.custom_hourly_target)
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
            if staff_type == "checker":
                # The owner's rule: an urgent BILL is worth more points, not
                # just an urgent item. With the weakest-link minimum below, the
                # bills figure only counted on days when it happened to be the
                # weaker one, so 10 urgent bills out of 30 scored exactly the
                # same as 30 normal ones. Averaging both (urgent weighted in
                # each) means every urgent bill and item always adds points.
                return round((items_r + bills_r) / 2.0, 3)
            # Both must be hit — use minimum (weakest link)
            return round(min(items_r, bills_r), 3)

        # Calculate VWCR for the role
        if staff_type == "picker":
            daily_vol_items = safe_div(ti, days)
        elif staff_type == "checker":
            daily_vol_items = safe_div(weighted_items, days)
        else:
            daily_vol_items = safe_div(tck_total, days)
        daily_vol_per_hr  = safe_div(daily_vol_items, 9.0)  # 9-hr workday
        if staff_type == "picker":
            vwcr = _vwcr(tp, ti, daily_vol_per_hr, PICKER_TARGET_HR)
        elif staff_type == "checker":
            # This used to be _vwcr(tck_normal, tck_total, ...), i.e. the share
            # of work that was NOT urgent -- which quietly punished whoever took
            # the urgent bills, the opposite of what we want. The ratio is now
            # weighted items over raw items, so it sits at 1.0 for all-normal
            # work and rises with the urgent share.
            vwcr = _vwcr(weighted_items, max(tck_total, 1), daily_vol_per_hr, CHECKER_TARGET_HR)
        else:
            # Purchaser VWCR: racked items accuracy × volume vs target
            pur_items_daily = safe_div(tck_urgent, days)  # tck_urgent = pur_items for purchaser
            pur_items_per_hr = safe_div(pur_items_daily, 9.0)
            vwcr = _vwcr(tro, max(tck_urgent, 1), pur_items_per_hr, 30.0)  # purchaser lighter target

        # Cleaner Rate
        if staff_type == "picker":
            cleaner_rate_score = _cleaner_rate(tp, max(ti, 1), tsd, safe_div(tro, max(days, 1)))
        elif staff_type == "checker":
            # The first argument is "clean" volume. For a picker that is items
            # picked rather than missed. For a checker it used to be tck_normal
            # -- the items that were NOT urgent -- which made this a second,
            # hidden penalty for taking urgent work on top of the VWCR one.
            # Urgent items are harder work, not spoiled work, so they count here
            # too, at their weight.
            cleaner_rate_score = _cleaner_rate(weighted_items, max(tck_total, 1), tsd, 0)
        else:
            cleaner_rate_score = _cleaner_rate(tro, max(tck_urgent, 1), tsd, 0)

        # Efficiency Ratio per day
        # For a checker, bills per day used to be `tsb` -- the NORMAL bills
        # field only -- so urgent bills were not counted here at all, and a
        # checker who took urgent bills scored LOWER than one who did the same
        # number of normal bills. Both figures now count urgent work, weighted.
        if staff_type == "checker":
            items_daily = safe_div(weighted_items, max(days, 1))
            bills_daily = safe_div(weighted_bills, max(days, 1))
        else:
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

        point_breakdown = []   # filled per role where the arithmetic is published
        pick_stats = None      # bill-based picking figures (pickers only)
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
            # Each component kept by name, so the profile and the PDF show the
            # points actually awarded instead of each re-deriving the formula.
            _pc = [
                ("Bill Processing", 25, (pur_bill_rate / 100) * 25,
                 "%d of %d PO bills checked (%.1f%%)" % (pur_bills_checked, pur_bills_received, pur_bill_rate)),
                ("CS Fulfilment", 20, (pur_cs_fulfilment / 100) * 20,
                 "%d of %d CS received (%.1f%%)" % (pur_cs_received, pur_cs_open, pur_cs_fulfilment)),
                ("Racking", 15, (pur_racking_eff / 100) * 15,
                 "%d of %d items racked (%.1f%%)" % (pur_items_racked, pur_items, pur_racking_eff)),
                ("Speed", 10, min(pur_speed / PURCHASER_TARGET_HR, 1.0) * 10,
                 "%.1f items/hr against %g" % (pur_speed, PURCHASER_TARGET_HR)),
                ("Bill Entry", 8, (pur_entry_rate / 100) * 8, "%.1f%% of PO bills entered" % pur_entry_rate),
                ("Pending Bills", 5, pur_pending_pct * 5, "%d PO bills not yet checked" % pur_pending_bills),
                ("VWCR", 8, (vwcr / 120) * 8, "%.1f of 120" % vwcr),
                ("Efficiency Ratio", 4, efficiency_ratio * 4, "%.3f" % efficiency_ratio),
                ("Workspace", 3, (workspace_score / 100) * 3, "%.1f%%" % workspace_score),
                ("Consistency", 2, (consistency / 100) * 2, "%.1f%%" % consistency),
            ]
            eff_score = round(sum(c[2] for c in _pc), 1)
            point_breakdown = [{"name": n, "max": m, "got": round(g, 1), "how": h} for n, m, g, h in _pc]

        elif staff_type == "picker" and primary_role == "picker":
            # ── PICKER: BILL-BASED PICKING (100 pts) ──
            # Scored from finished grabs, not typed totals:
            #   On time 40 · Speed 30 · Volume 20 · Consistency 10
            _ps = picker_pick_stats(emp_id, [e.entry_date for e in entries])
            pick_ontime_pct = round(100.0 * _ps["on_time"] / _ps["grabs"], 1) if _ps["grabs"] else 0.0
            pick_rate = round(_ps["items"] / (_ps["seconds"] / 3600.0), 1) if _ps["seconds"] else 0.0
            pick_items_day = round(_ps["items"] / max(days, 1), 1)
            _rates = list(_ps["daily_rate"].values())
            if len(_rates) > 1:
                _m = sum(_rates) / len(_rates)
                _sd = (sum((r - _m) ** 2 for r in _rates) / len(_rates)) ** 0.5
                pick_consistency = round(max(0.0, 100.0 - (_sd / max(_m, 1)) * 100), 1)
            else:
                pick_consistency = 100.0 if _rates else 0.0
            pick_stats = dict(_ps, on_time_pct=pick_ontime_pct, rate=pick_rate,
                              items_per_day=pick_items_day, consistency=pick_consistency)
            _pc = [
                ("On time", 40, (pick_ontime_pct / 100) * 40,
                 "%d of %d grabs done within the timer (%.1f%%)" % (_ps["on_time"], _ps["grabs"], pick_ontime_pct)),
                ("Pick speed", 30, min(pick_rate / PICKER_TARGET_HR, 1.0) * 30,
                 "%.1f items/hr while picking, against %.0f" % (pick_rate, PICKER_TARGET_HR)),
                ("Volume", 20, min(pick_items_day / HARD_ITEMS_PER_DAY, 1.0) * 20,
                 "%.1f items a day, against %d" % (pick_items_day, HARD_ITEMS_PER_DAY)),
                ("Consistency", 10, (pick_consistency / 100) * 10,
                 "%.1f%% — steadiness of the daily pick rate" % pick_consistency),
            ]
            eff_score = round(sum(c[2] for c in _pc), 1)
            point_breakdown = [{"name": n, "max": m, "got": round(g, 1), "how": h} for n, m, g, h in _pc]
        elif staff_type == "picker":
            # ── PICKER POINT SYSTEM (100 pts total) — Phase 1 rebalance ──
            # Still used when picker is someone's second role (multitask).
            # OLD: Acc×25 + Fulfil×20 + Speed×18 + PackEff×12 + Workspace×10 + CS×8 + Consistency×7
            # NEW: VWCR×25 + Fulfil×18 + Speed×15 + EffRatio×10 + Workspace×10
            #    + CS×8 + Cleaner×7 + LogCons×7
            # (Packing Efficiency removed; 12 pts redistributed to VWCR+6, EffRatio+4, LogCons+2)
            # pick_speed is items per HOUR, scored against the hourly target (it
            # was once divided by 250, a per-DAY figure, so nobody earned it).
            _pc = [
                ("VWCR", 25, (vwcr / 120) * 25, "%.1f of 120 — accuracy x volume against target" % vwcr),
                ("Bill Fulfilment", 18, (bill_fulfilment / 100) * 18,
                 "%d of %d bills picked (%.1f%%)" % (tsb_normal, tbr_picker, bill_fulfilment)),
                ("Pick Speed", 15, min(pick_speed / PICKER_TARGET_HR, 1.0) * 15,
                 "%.1f items/hr against %.0f target" % (pick_speed, PICKER_TARGET_HR)),
                ("Efficiency Ratio", 10, efficiency_ratio * 10,
                 "%.3f — weakest of items/day and bills/day against 200 / 50" % efficiency_ratio),
                ("Workspace", 10, (workspace_score / 100) * 10, "%.1f%% — rack, table, sweep" % workspace_score),
                ("CS Fulfilment", 8, min(cs_fulfilment / 100, 1.0) * 8, "%.1f%%" % cs_fulfilment),
                ("Cleaner Rate", 7, (cleaner_rate_score / 100) * 7, "%.1f%%" % cleaner_rate_score),
                ("Consistency", 7, (consistency / 100) * 7, "%.1f%% — steadiness of daily accuracy" % consistency),
            ]
            eff_score = round(sum(c[2] for c in _pc), 1)
            point_breakdown = [{"name": n, "max": m, "got": round(g, 1), "how": h} for n, m, g, h in _pc]
        else:
            # CHECKER POINT SYSTEM (100 pts total). Reached only when staff_type
            # resolved to "checker" above, never as a silent catch-all.
            # Volume: scored on the WEIGHTED speed, so urgent items pay more.
            # The divisor used to be a hard-coded 80 while VWCR below scored the
            # same person against CHECKER_TARGET_HR (70) -- two different targets
            # for the same work. Both now use the configured target.
            speed_score     = min(safe_div(check_speed_weighted, CHECKER_TARGET_HR), 1.0) * 22
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
            # Shown on the staff page so the arithmetic is inspectable rather
            # than a number that appears from nowhere.
            point_breakdown = [
                {"name": "Weighted Speed", "max": 22, "got": round(speed_score, 1),
                 "how": "%.1f weighted items/hr ÷ %.0f target (urgent counted ×%.2g)"
                        % (check_speed_weighted, CHECKER_TARGET_HR, urgent_weight)},
                {"name": "Team Clearance", "max": 22, "got": round(clearance_score, 1),
                 "how": "%d of %d bills cleared by the team (%.1f%%), %d left pending"
                        % (team_checked_bills, tbr, clearance_rate, team_pending_bills)},
                {"name": "VWCR", "max": 20, "got": round(vwcr_score, 1),
                 "how": "%.1f of 120 — volume against target, lifted by urgent share" % vwcr},
                {"name": "Cleaner Rate", "max": 8, "got": round(cleaner_score, 1),
                 "how": "%.1f%%" % cleaner_rate_score},
                {"name": "Efficiency Ratio", "max": 8, "got": round(eff_ratio_score, 1),
                 "how": "%.3f — average of %.0f items/day and %.0f bills/day against %d / %d "
                        "(urgent counted ×%.2g in both)"
                        % (efficiency_ratio, items_daily, bills_daily, HARD_ITEMS_PER_DAY,
                           HARD_BILLS_PER_DAY, urgent_weight)},
                {"name": "Log Consistency", "max": 8, "got": round(consist_score, 1),
                 "how": "%.1f%% — steadiness of the daily rate" % consistency},
                {"name": "Potential Efficiency", "max": 5, "got": round(poteff_score, 1),
                 "how": "%.1f%% of the theoretical ceiling" % potential_eff},
                {"name": "Workspace", "max": 4, "got": round(workspace_ck, 1),
                 "how": "%.1f%% — rack, table, sweep" % workspace_score},
                {"name": "Sweep Days", "max": 3, "got": round(sweep_score, 1),
                 "how": "%d of %d days" % (tsd, days)},
            ]
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

        # Supervisor mismatch penalty (current month, only after 5 mismatches)
        supervisor_penalty = 0.0
        supervisor_mismatches = 0
        try:
            if emp_id is not None:
                _ym = date.today().strftime("%Y-%m")
                _ss = req_memo("strikes", (emp_id, _ym),
                               lambda: supervisor_strike_summary(emp_id, _ym))
                supervisor_penalty = float(_ss.get("penalty", 0.0))
                supervisor_mismatches = int(_ss.get("mismatches", 0))
        except Exception:
            supervisor_penalty = 0.0

        # Multitask bonus: +10 per extra role worked, capped at +30 for a single day.
        # Averaged over the days in this period so it stays on the same 0-100 scale as the
        # rest of eff_score: a one-day window gets that day's bonus outright, while a month
        # window gets the mean of its daily bonuses. Computed here rather than in the
        # dashboard route so the bonus reaches the admin view, the leaderboards and the
        # monthly reports as well as the staff member's own card.
        multitask_bonus = 0.0
        multitask_days = 0            # days in this period with extra-role work
        multitask_day_points = 0      # points earned on those days, before averaging
        multitask_breakdown = []      # one row per day: which roles, how many points
        try:
            if emp_id is not None:
                _dates = {e.entry_date for e in entries if e.entry_date}
                if _dates:
                    if has_request_context():
                        def _load_mt():
                            by = {}
                            for _m in MultitaskEntry.query.all():
                                by.setdefault(_m.emp_id, []).append(_m)
                            return by
                        _mts = [m for m in req_memo("mt_by_emp", 0, _load_mt).get(emp_id, [])
                                if m.entry_date in _dates]
                    else:
                        _mts = MultitaskEntry.query.filter(
                            MultitaskEntry.emp_id == emp_id,
                            MultitaskEntry.entry_date.in_(list(_dates)),
                        ).all()
                    _roles_by_day = {}
                    for _mt in _mts:
                        _roles_by_day.setdefault(_mt.entry_date, set()).add(_mt.secondary_type)
                    if _roles_by_day:
                        _total = sum(min(10 * len(rs), 30) for rs in _roles_by_day.values())
                        multitask_days = len(_roles_by_day)
                        multitask_day_points = _total
                        multitask_bonus = round(_total / len(_dates), 1)
                        for _d, _rs in sorted(_roles_by_day.items(), reverse=True):
                            multitask_breakdown.append({
                                "date": _d, "roles": sorted(_rs),
                                "raw": 10 * len(_rs), "points": min(10 * len(_rs), 30)})
        except Exception as _mtb:
            logger.warning(f"multitask bonus: {_mtb}")
            multitask_bonus = 0.0

        # Keep the raw formula output before any adjustment, so the admin can be
        # shown exactly how the final number was reached instead of one opaque total.
        base_score = round(eff_score, 1)

        eff_score = max(round(eff_score - complaint_deduction + total_adjustment
                              - supervisor_penalty + multitask_bonus, 1), 0.0)
        eff_score = min(eff_score, 100.0)

        # A role with no KPI point system is reported, never graded on another
        # role's maths.
        if not is_scored:
            base_score = 0.0
            eff_score = 0.0

        # Targets that rise with performance.
        # The old rule raised a target by a flat 40% when the "error rate" was
        # under 1% -- and for a checker it used the URGENT share as the error
        # rate (urgent work is harder, not wrong) and needed picker item counts,
        # so it could never fire for a checker at all. The suggestion is now the
        # person's own 75th-percentile daily pace over the period: a bar they
        # demonstrably reach on good days, which moves up as they improve.
        auto_suggest = None
        target_suggestion = None
        if is_scored and staff_type in ("picker", "checker") and days >= 5:
            if staff_type == "picker" and pick_stats is not None:
                _rates = list(pick_stats["daily_rate"].values())
            elif staff_type == "picker":
                _rates = [((e.picked or 0) + (e.missed or 0)) / FIXED_HRS for e in entries]
            else:
                _rates = [((e.checked or 0) + (e.errors_found or 0) * urgent_weight) / FIXED_HRS
                          for e in entries]
            _rates = sorted(r for r in _rates if r > 0)
            if len(_rates) >= 5:
                _p75 = round(_rates[int(0.75 * (len(_rates) - 1))], 1)
                _current = PICKER_TARGET_HR if staff_type == "picker" else CHECKER_TARGET_HR
                _custom = bool(_emp_for_target and _emp_for_target.custom_hourly_target)
                if _custom and _p75 > _current * 1.02:
                    target_suggestion = {"current": round(_current, 1), "suggested": _p75,
                                         "kind": "raise", "days": len(_rates)}
                    auto_suggest = (f"Pace has risen: 75th-percentile day is {_p75}/hr over "
                                    f"{len(_rates)} days. Raise target from {_current:g} to {_p75:g}/hr.")
                elif not _custom and _p75 < _current:
                    target_suggestion = {"current": round(_current, 1), "suggested": _p75,
                                         "kind": "set", "days": len(_rates)}
                    auto_suggest = (f"The role target of {_current:g}/hr is above anything reached. "
                                    f"Set a personal target of {_p75:g}/hr (75th-percentile day over "
                                    f"{len(_rates)} days); it will then rise as pace improves.")

        # Grade thresholds (unified across roles)

        # Unified grade thresholds
        if   eff_score >= 88: grade = "ELITE"
        elif eff_score >= 72: grade = "PROFICIENT"
        elif eff_score >= 52: grade = "SATISFACTORY"
        else:                 grade = "RE-TRAINING"
        # Never label a role that has no point system as needing retraining.
        if not is_scored:
            grade = "NOT SCORED"

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
            # Team-wide checker figures. pending_bills is the TEAM's leftover,
            # the same number for every checker on the day, and pending_manual
            # is only the old hand-typed figure kept for comparison.
            team_total_bills=tbr, team_checked_bills=team_checked_bills,
            team_pending_bills=team_pending_bills, team_urgent_bills=team_urgent_bills,
            team_checker_count=team_checker_count, team_share_pct=team_share_pct,
            team_per_person_days=team_per_person_days, team_conflict_days=team_conflict_days,
            pending_manual=tpbm,
            # Urgent weighting
            urgent_weight=urgent_weight, weighted_items=weighted_items,
            weighted_bills=weighted_bills, urgent_item_bonus=urgent_item_bonus,
            urgent_bill_bonus=urgent_bill_bonus,
            check_speed_weighted=check_speed_weighted,
            point_breakdown=point_breakdown,
            pick_stats=pick_stats,
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
            supervisor_penalty=round(supervisor_penalty, 1),
            supervisor_mismatches=supervisor_mismatches,
            multitask_bonus=multitask_bonus,
            base_score=base_score,
            is_scored=is_scored,
            primary_role=primary_role,
            scored_as=staff_type,
            multitask_days=multitask_days,
            multitask_day_points=multitask_day_points,
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
            auto_suggest=auto_suggest, target_suggestion=target_suggestion,
            multitask_breakdown=multitask_breakdown,
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
    # "month" is the CALENDAR month (1st to today), the same as the admin
    # dashboard and the label on the PDF. It used to be the last 30 days here
    # while the admin table used the calendar month, so one person's "month"
    # score differed from screen to screen. "last30" is a rolling window for
    # judging pace (targets), which needs enough days even on the 2nd.
    starts = {"day": today, "week": today - timedelta(days=6),
              "month": today.replace(day=1), "last30": today - timedelta(days=29)}
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


def _entry_volume(e, staff_type):
    """Primary output volume of one KPIEntry, by role."""
    st = (staff_type or "").lower()
    if st == "picker":
        return (e.picked or 0)
    if st == "checker":
        return (e.checked or 0)
    if st == "purchaser":
        return (e.checked or 0)  # PO bills checked
    return (e.sales_bills_open or 0)


def compute_monthly_target(emp_id, staff_type):
    """Auto monthly target = the employee's own highest past calendar-month total.
    Their personal best month becomes the bar to beat next month.
    Returns dict with best_total, best_label, current_total, pct, met."""
    try:
        entries = KPIEntry.query.filter_by(emp_id=emp_id).all()
    except Exception:
        entries = []
    if not entries:
        return None
    today = date.today()
    cur_key = (today.year, today.month)
    by_month = {}
    for e in entries:
        if not e.entry_date:
            continue
        key = (e.entry_date.year, e.entry_date.month)
        by_month[key] = by_month.get(key, 0) + _entry_volume(e, staff_type)
    current_total = by_month.get(cur_key, 0)
    # Best among PAST months (exclude the current, still-in-progress month)
    past = {k: v for k, v in by_month.items() if k != cur_key}
    if not past:
        return {"best_total": 0, "best_label": "—", "current_total": current_total,
                "pct": 0, "met": False, "has_history": False}
    best_key = max(past, key=lambda k: past[k])
    best_total = past[best_key]
    best_label = date(best_key[0], best_key[1], 1).strftime("%B %Y")
    pct = round(current_total / best_total * 100, 1) if best_total else 0
    return {"best_total": best_total, "best_label": best_label,
            "current_total": current_total, "pct": min(pct, 999),
            "met": current_total >= best_total and best_total > 0,
            "has_history": True}


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

        # Team figures per day for the log: pending is the day's total minus
        # every checker's checked bills, the same as on the profile.
        team_days = (checker_team_day_totals({e.entry_date for e in recent_30})
                     if emp.staff_type == "checker" else {})

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
            "team_days":    team_days,
            "all_entries":  recent_30,
        }
    except Exception as e:
        logger.error(f"build_pdf_payload: {e}")
        return {"staff_type": emp.staff_type, "all_entries": []}


# ─── Security: headers + brute-force throttling ─────────────────────────────

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")              # no clickjacking
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")    # no MIME sniffing
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=()")
    if IS_PRODUCTION:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# In-memory failed-attempt throttle (per key). Fine for a single-instance app;
# resets on restart, which only ever helps the legitimate user.
_fail_counts = {}   # key -> [count, locked_until_timestamp]
_THROTTLE_MAX = 5
_THROTTLE_LOCK_SECS = 600   # 10 minutes

def _throttle_check(key):
    """Returns remaining lock seconds if locked, else 0."""
    rec = _fail_counts.get(key)
    if not rec:
        return 0
    import time as _t
    if rec[0] >= _THROTTLE_MAX and rec[1] > _t.time():
        return int(rec[1] - _t.time())
    if rec[1] and rec[1] <= _t.time():
        _fail_counts.pop(key, None)   # lock expired — reset
    return 0

def _throttle_fail(key):
    import time as _t
    rec = _fail_counts.setdefault(key, [0, 0])
    rec[0] += 1
    if rec[0] >= _THROTTLE_MAX:
        rec[1] = _t.time() + _THROTTLE_LOCK_SECS

def _throttle_clear(key):
    _fail_counts.pop(key, None)


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
            "multitask_bonus": 0.0, "base_score": 0.0, "is_scored": True,
            "supervisor_penalty": 0.0, "multitask_days": 0, "multitask_day_points": 0,
            "vwcr": 0, "cleaner_rate": 0, "efficiency_ratio": 0, "is_efficient": False,
            "items_daily": 0, "bills_daily": 0,
            "hard_target_items": 200, "hard_target_bills": 50,
            "hourly_target": 50, "auto_suggest": None, "target_suggestion": None,
            "multitask_breakdown": [],
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
            "team_total_bills": 0, "team_checked_bills": 0, "team_pending_bills": 0,
            "team_urgent_bills": 0, "team_checker_count": 0, "team_share_pct": 0,
            "team_per_person_days": 0, "team_conflict_days": 0,
            "pending_manual": 0,
            "urgent_weight": URGENT_BILL_WEIGHT, "weighted_items": 0,
            "weighted_bills": 0, "urgent_item_bonus": 0, "urgent_bill_bonus": 0,
            "check_speed_weighted": 0, "point_breakdown": [],
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


PUSH_ASYNC = True          # tests switch this off to send inline


def vapid_keys():
    """(public, private) push keys: from VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY,
    or made once and kept in the database, so nothing has to be set up."""
    pub, priv = os.environ.get("VAPID_PUBLIC_KEY", ""), os.environ.get("VAPID_PRIVATE_KEY", "")
    if pub and priv:
        return pub, priv
    a, b = db.session.get(AppKey, "vapid_public"), db.session.get(AppKey, "vapid_private")
    if a and b:
        return a.value, b.value
    import webpush
    pub, priv = webpush.new_keys()
    db.session.add(AppKey(name="vapid_public", value=pub))
    db.session.add(AppKey(name="vapid_private", value=priv))
    db.session.commit()
    return pub, priv


def push_now(emp_id, title, body="", link=None):
    """Send an alert to every phone/browser this employee turned alerts on
    for. Gone subscriptions (404/410) are removed. Returns how many sent."""
    import webpush
    sent = 0
    try:
        subs = PushSub.query.filter_by(emp_id=emp_id).all()
        if not subs:
            return 0
        pub, priv = vapid_keys()
        subject = "mailto:" + (os.environ.get("ADMIN_EMAIL") or "admin@pharmaip.com")
        data = {"title": title[:120], "body": (body or "")[:300], "link": link or "/dashboard",
                "tag": "ip-%d" % int(time.time() * 1000)}
        for s_ in subs:
            try:
                code = webpush.send({"endpoint": s_.endpoint, "keys": {"p256dh": s_.p256dh, "auth": s_.auth}},
                                    data, pub, priv, subject)
                if code in (404, 410):
                    db.session.delete(s_)
                elif 200 <= code < 300:
                    sent += 1
                else:
                    logger.warning("push to %s: HTTP %s", emp_id, code)
            except Exception as exc:
                logger.warning("push to %s failed: %s", emp_id, exc)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning("push_now: %s", exc)
    return sent


def push_to_employee(emp_id, title, body="", link=None):
    """Phone alert without slowing the request down: sent in the background."""
    if not PUSH_ASYNC:
        return push_now(emp_id, title, body, link)
    def _job():
        with app.app_context():
            push_now(emp_id, title, body, link)
    try:
        socketio.start_background_task(_job)
    except Exception as exc:
        logger.warning("push background: %s", exc)


def notify_employee(emp_id, title, body="", link=None, sms=True):
    """Create an in-app notification and optionally send an SMS (if the employee has a phone).
    Best-effort — never raises so callers don't break."""
    try:
        n = Notification(emp_id=emp_id, title=title[:150], body=(body or "")[:400], link=link)
        db.session.add(n)
        db.session.commit()
        push_to_employee(emp_id, title, body, link)
    except Exception as e:
        db.session.rollback()
        logger.warning(f"notify_employee in-app failed: {e}")
    if sms:
        try:
            emp = db.session.get(Employee, emp_id)
            if emp and getattr(emp, "phone", None):
                msg = f"{title}" + (f" — {body}" if body else "")
                send_sms(emp.phone, msg[:300])
        except Exception as e:
            logger.warning(f"notify_employee SMS failed: {e}")


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
                ("phone",           "VARCHAR(20)"),
                ("secondary_staff_type", "VARCHAR(20)"),
                ("extra_roles",     "VARCHAR(160)"),
                ("created_at",      "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ]
            for col, col_type in emp_cols:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE employees ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                    db.session.commit()
                    logger.info(f"Column '{col}' ensured on employees table")
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
                logger.info("Complaints table ensured")
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
                    logger.info(f"Column '{col}' ensured on kpi_entries table")
                except Exception as ce:
                    db.session.rollback()
                    logger.warning(f"kpi_entries column '{col}' migration skipped: {ce}")
            # Add target_emp_id to complaints (targeted minus-marking)
            try:
                db.session.execute(db.text(
                    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS target_emp_id INTEGER REFERENCES employees(id)"
                ))
                db.session.commit()
                logger.info("complaints.target_emp_id ensured")
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
                logger.info("bill_validations table ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"bill_validations migration skipped: {ce}")
            # shift_note column on kpi_entries
            try:
                db.session.execute(db.text(
                    "ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS shift_note VARCHAR(300)"
                ))
                db.session.commit()
                logger.info("kpi_entries.shift_note ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"kpi_entries.shift_note migration skipped: {ce}")
            # Add phone column to employees (Feature 15)
            try:
                db.session.execute(db.text(
                    "ALTER TABLE employees ADD COLUMN IF NOT EXISTS phone VARCHAR(20)"
                ))
                db.session.commit()
                logger.info("employees.phone ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"employees.phone migration skipped: {ce}")
            # Secondary staff type
            try:
                db.session.execute(db.text(
                    "ALTER TABLE employees ADD COLUMN IF NOT EXISTS secondary_staff_type VARCHAR(20)"
                ))
                db.session.commit()
                logger.info("employees.secondary_staff_type ensured")
            except Exception as ce:
                db.session.rollback()
                logger.warning(f"employees.secondary_staff_type migration skipped: {ce}")
            # Delivery CRM (Excel-format) columns on delivery_assignments
            for col, col_type in [
                ("route", "VARCHAR(300)"),
                ("no_of_tasks", "INTEGER DEFAULT 1"),
                ("packet_type", "VARCHAR(50)"),
                ("dispatch_time", "TIMESTAMP"),
                ("no_of_task_return", "INTEGER DEFAULT 0"),
                ("return_reason", "VARCHAR(200)"),
            ]:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE delivery_assignments ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                    db.session.commit()
                except Exception as ce:
                    db.session.rollback()
                    logger.warning(f"delivery_assignments.{col} migration skipped: {ce}")
            logger.info("delivery_assignments CRM columns ensured")
            # Per-store packet type + distance on delivery_stops; workspace + extra_roles
            for tbl, col, col_type in [
                ("delivery_stops", "packet_type", "VARCHAR(50)"),
                ("delivery_stops", "dist_from_prev_km", "FLOAT"),
                ("delivery_stops", "area", "VARCHAR(150)"),
                ("delivery_biller_notes", "table_clean", "INTEGER DEFAULT 0"),
                ("delivery_biller_notes", "sweep_done", "INTEGER DEFAULT 0"),
                ("employees", "extra_roles", "VARCHAR(160)"),
                ("multitask_entries", "bills_done", "INTEGER DEFAULT 0"),
                ("multitask_entries", "items_count", "INTEGER DEFAULT 0"),
                ("multitask_entries", "breakage_received", "INTEGER DEFAULT 0"),
                ("multitask_entries", "expire_received", "INTEGER DEFAULT 0"),
                ("multitask_entries", "breakage_expire_processed", "INTEGER DEFAULT 0"),
                ("multitask_entries", "item_receive_qty", "INTEGER DEFAULT 0"),
                ("multitask_entries", "expire_return_qty", "INTEGER DEFAULT 0"),
            ]:
                try:
                    db.session.execute(db.text(
                        f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                    db.session.commit()
                except Exception as ce:
                    db.session.rollback()
                    logger.warning(f"{tbl}.{col} migration skipped: {ce}")
            logger.info("delivery_stops / notes / extra_roles columns ensured")
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
                """CREATE TABLE IF NOT EXISTS delivery_stops (
                    id SERIAL PRIMARY KEY,
                    assignment_id INTEGER NOT NULL REFERENCES delivery_assignments(id) ON DELETE CASCADE,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    place_name VARCHAR(150) NOT NULL,
                    packages INTEGER DEFAULT 0,
                    reached_at TIMESTAMP,
                    minutes_from_prev FLOAT,
                    lat FLOAT,
                    lng FLOAT,
                    note VARCHAR(300))""",
                """CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    emp_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    title VARCHAR(150) NOT NULL,
                    body VARCHAR(400),
                    link VARCHAR(200),
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS supervisor_bills (
                    id SERIAL PRIMARY KEY,
                    staff_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    entry_date DATE NOT NULL,
                    super_bills INTEGER DEFAULT 0,
                    supervisor_id INTEGER REFERENCES employees(id),
                    admin_validated BOOLEAN DEFAULT FALSE,
                    note VARCHAR(200),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT _supbill_staff_date_uc UNIQUE(staff_id, entry_date))""",
                """CREATE TABLE IF NOT EXISTS breadcrumb_points (
                    id SERIAL PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES delivery_trips(id) ON DELETE CASCADE,
                    lat FLOAT NOT NULL,
                    lng FLOAT NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
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
            _raw = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
            # Flask-SQLAlchemy resolves relative paths against the instance folder
            if not os.path.isabs(_raw):
                db_path = os.path.join(app.instance_path, _raw)
            else:
                db_path = _raw
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
                "extra_roles": "VARCHAR(160)",  # multitasker: many roles
            }
            for col, col_type in sqlite_cols.items():
                if col not in existing:
                    cursor.execute(f"ALTER TABLE employees ADD COLUMN {col} {col_type}")
                    logger.info(f"SQLite column '{col}' added")
            # KPI entries sqlite
            try:
                cursor.execute("PRAGMA table_info(kpi_entries)")
                kpi_existing = [row[1] for row in cursor.fetchall()]
                if "admin_adjustment" not in kpi_existing:
                    cursor.execute("ALTER TABLE kpi_entries ADD COLUMN admin_adjustment FLOAT DEFAULT 0")
                    logger.info("SQLite kpi_entries.admin_adjustment added")
            except Exception as kce:
                logger.warning(f"SQLite kpi_entries migration: {kce}")
            # complaints.target_emp_id
            try:
                cursor.execute("PRAGMA table_info(complaints)")
                cmp_cols = [row[1] for row in cursor.fetchall()]
                if cmp_cols and "target_emp_id" not in cmp_cols:
                    cursor.execute("ALTER TABLE complaints ADD COLUMN target_emp_id INTEGER")
                    logger.info("SQLite complaints.target_emp_id added")
            except Exception as sce:
                logger.warning(f"SQLite complaints migration: {sce}")
            # shift_note on kpi_entries
            try:
                cursor.execute("PRAGMA table_info(kpi_entries)")
                kpi_cols2 = [row[1] for row in cursor.fetchall()]
                if "shift_note" not in kpi_cols2:
                    cursor.execute("ALTER TABLE kpi_entries ADD COLUMN shift_note VARCHAR(300)")
                    logger.info("SQLite kpi_entries.shift_note added")
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
                logger.info("SQLite admin_staff_notes ensured")
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
                logger.info("SQLite password_reset_otps ensured")
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
                logger.info("SQLite monthly_report_archives ensured")
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
                               "total_bills_received", "bills_received", "pending_bills_manual",
                               "quantity", "bills_done", "items_count",
                               "breakage_received", "expire_received", "breakage_expire_processed",
                               "item_receive_qty", "expire_return_qty"]:
                    if mt_col not in mt_existing:
                        cursor.execute(f"ALTER TABLE multitask_entries ADD COLUMN {mt_col} INTEGER DEFAULT 0")
                logger.info("SQLite multitask_entries ensured")
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
                logger.info("SQLite delivery_biller_notes ensured")
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
                cursor.execute("""CREATE TABLE IF NOT EXISTS delivery_stops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    emp_id INTEGER NOT NULL,
                    place_name VARCHAR(150) NOT NULL,
                    packages INTEGER DEFAULT 0,
                    reached_at TIMESTAMP,
                    minutes_from_prev REAL,
                    lat REAL,
                    lng REAL,
                    note VARCHAR(300))""")
                cursor.execute("""CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id INTEGER NOT NULL,
                    title VARCHAR(150) NOT NULL,
                    body VARCHAR(400),
                    link VARCHAR(200),
                    is_read INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                cursor.execute("""CREATE TABLE IF NOT EXISTS supervisor_bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    staff_id INTEGER NOT NULL,
                    entry_date DATE NOT NULL,
                    super_bills INTEGER DEFAULT 0,
                    supervisor_id INTEGER,
                    admin_validated INTEGER DEFAULT 0,
                    note VARCHAR(200),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(staff_id, entry_date))""")
                # Add CRM columns to existing SQLite delivery_assignments (ignore if present)
                for col, col_type in [
                    ("route", "VARCHAR(300)"), ("no_of_tasks", "INTEGER DEFAULT 1"),
                    ("packet_type", "VARCHAR(50)"), ("dispatch_time", "TIMESTAMP"),
                    ("no_of_task_return", "INTEGER DEFAULT 0"), ("return_reason", "VARCHAR(200)"),
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE delivery_assignments ADD COLUMN {col} {col_type}")
                    except Exception:
                        pass
                # Per-store packet type + distance + area on delivery_stops
                for col, col_type in [("packet_type", "VARCHAR(50)"), ("dist_from_prev_km", "REAL"), ("area", "VARCHAR(150)")]:
                    try:
                        cursor.execute(f"ALTER TABLE delivery_stops ADD COLUMN {col} {col_type}")
                    except Exception:
                        pass
                # Workspace (table clean / sweep) on delivery_biller_notes — for everyone
                for col, col_type in [("table_clean", "INTEGER DEFAULT 0"), ("sweep_done", "INTEGER DEFAULT 0")]:
                    try:
                        cursor.execute(f"ALTER TABLE delivery_biller_notes ADD COLUMN {col} {col_type}")
                    except Exception:
                        pass
                # Breadcrumb GPS trail table
                cursor.execute("""CREATE TABLE IF NOT EXISTS breadcrumb_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trip_id INTEGER NOT NULL REFERENCES delivery_trips(id) ON DELETE CASCADE,
                    lat REAL NOT NULL,
                    lng REAL NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                conn.commit()
                logger.info("SQLite delivery tables created")
            except Exception as dte:
                logger.warning(f"SQLite delivery tables: {dte}")
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Migration error: {e}")


def secure_supabase_exposure():
    """Close the Supabase Data API off from this app's tables. PostgreSQL only.

    Supabase puts a public REST API (PostgREST) in front of the "public" schema
    and hands out an anon key that is designed to be embedded in browsers. That
    is safe only when Row-Level Security is on. With RLS off -- which is how
    these tables were created, and what Supabase's advisor flagged as
    rls_disabled_in_public -- anyone holding the project URL and that public key
    can read, edit and delete every row: staff names, phone numbers, scores and
    the riders' GPS trails.

    This app never uses the Data API. It talks to Postgres directly over
    DATABASE_URL, as the table owner, and an owner bypasses RLS. So enabling RLS
    costs the app nothing and shuts the public door. The grants to anon and
    authenticated are revoked as well, so the tables are not merely protected by
    a policy check but absent from the API surface entirely.

    FORCE ROW LEVEL SECURITY is deliberately NOT used: that would apply RLS to
    the owner too and lock the application out of its own data.
    """
    try:
        if db.engine.dialect.name != "postgresql":
            return []      # SQLite (or anything else): there is no Data API to close
    except Exception:
        return []
    done, failed = [], []
    try:
        tables = sorted({m.class_.__table__.name for m in db.Model.registry.mappers})
    except Exception as e:
        logger.error(f"secure_supabase_exposure: cannot list tables: {e}")
        return []
    for t in tables:
        q = '"%s"' % t.replace('"', '')
        for stmt in (
            f"ALTER TABLE public.{q} ENABLE ROW LEVEL SECURITY",
            f"REVOKE ALL ON public.{q} FROM anon",
            f"REVOKE ALL ON public.{q} FROM authenticated",
        ):
            try:
                db.session.execute(db.text(stmt))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                # anon/authenticated may not exist outside Supabase; not an error.
                if "does not exist" not in str(e).lower():
                    failed.append(f"{t}: {str(e)[:70]}")
        done.append(t)
    if failed:
        logger.warning("secure_supabase_exposure issues: " + "; ".join(failed[:5]))
    logger.info("Supabase exposure closed on %d table(s): RLS on, anon/authenticated "
                "revoked. The app is unaffected; it connects as the table owner."
                % len(done))
    return done


def reconcile_schema():
    """Add any column a model declares that the live table is missing.

    The hand-written migration blocks above list columns explicitly, so every
    time a model gained a field someone had to remember to add it there too.
    When that was missed the table stayed behind and every query touching the
    new column raised, which the callers swallow into an empty list -- the
    symptom being a panel that renders blank with no error on screen.

    This closes that gap generically: whatever the models declare is what the
    database gets. It only ever ADDS columns, never drops or retypes one, so it
    is safe to run on every boot and safe to run twice. New columns are added
    nullable regardless of the model's nullable flag, because an existing table
    with rows in it cannot take a NOT NULL column without a backfill.
    """
    from sqlalchemy import inspect as sa_inspect
    try:
        insp = sa_inspect(db.engine)
        existing = set(insp.get_table_names())
    except Exception as e:
        logger.error(f"reconcile_schema: cannot inspect database: {e}")
        return []

    prep = db.engine.dialect.identifier_preparer
    added = []
    for mapper in db.Model.registry.mappers:
        table = mapper.class_.__table__
        if table.name not in existing:
            continue  # db.create_all() builds missing tables in full
        try:
            live = {c["name"] for c in insp.get_columns(table.name)}
        except Exception as e:
            logger.warning(f"reconcile_schema: cannot read {table.name}: {e}")
            continue
        for col in table.columns:
            if col.name in live:
                continue
            try:
                col_type = col.type.compile(db.engine.dialect)
            except Exception:
                col_type = "VARCHAR(255)"
            clause = f"{prep.quote(table.name)} ADD COLUMN {prep.quote(col.name)} {col_type}"
            # Carry over a simple scalar default so existing rows get a sane value.
            default = getattr(col.default, "arg", None) if col.default is not None else None
            if isinstance(default, bool):
                clause += f" DEFAULT {'TRUE' if default else 'FALSE'}"
            elif isinstance(default, (int, float)):
                clause += f" DEFAULT {default}"
            elif isinstance(default, str):
                clause += " DEFAULT " + sa_text_literal(default)
            try:
                db.session.execute(db.text(f"ALTER TABLE {clause}"))
                db.session.commit()
                added.append(f"{table.name}.{col.name}")
            except Exception as e:
                db.session.rollback()
                logger.warning(f"reconcile_schema: {table.name}.{col.name} not added: {e}")

    if added:
        logger.warning("reconcile_schema added missing columns: " + ", ".join(added))
    else:
        logger.info("reconcile_schema: database schema matches the models")
    return added


def sa_text_literal(value):
    """Quote a string for inline use in DDL (defaults only, never user input)."""
    return "'" + str(value).replace("'", "''") + "'"


TRIAL_STAFF = [("Ravi", "pick_assigner"), ("Lisa", "picker"), ("Smriti", "picker"),
               ("Deepti", "picker"), ("Amit", "picker")]


def seed_trial_demo():
    """Ready-made accounts for a trial copy of the app. Runs only when
    TRIAL_DEMO=1 AND no DATABASE_URL is set, so it can never touch the live
    Supabase database. Passwords are the ADMIN_PASSWORD value; none is
    written in code. Safe to run twice."""
    if not (os.environ.get("TRIAL_DEMO", "0") == "1" and not os.environ.get("DATABASE_URL")):
        return 0
    pw = os.environ.get("ADMIN_PASSWORD", "admin123")
    made = 0
    for name, role in TRIAL_STAFF:
        email = "%s@pharmaip.com" % name.lower()
        if Employee.query.filter_by(email=email).first():
            continue
        e = Employee(name=name, email=email, staff_type=role, role=ROLE_DISPLAY_NAMES.get(role, role))
        e.set_password(pw)
        db.session.add(e)
        made += 1
    db.session.commit()
    if made:
        logger.info("Trial copy: %d test accounts created", made)
    return made


TRIAL_BANNER = ('<div role="note" style="position:sticky;top:0;z-index:9999;background:#7A5208;color:#fff;'
                'font:600 13px/1.4 system-ui,sans-serif;text-align:center;padding:6px 12px">'
                'Trial copy: not the live app. Data resets when the server restarts.</div>')


@app.after_request
def _trial_banner(resp):
    """On a trial copy, put a banner at the top of every page."""
    try:
        if (os.environ.get("TRIAL_DEMO", "0") == "1" and not os.environ.get("DATABASE_URL")
                and resp.mimetype == "text/html" and not resp.direct_passthrough):
            html = resp.get_data(as_text=True)
            i = html.find("<body")
            if i != -1:
                j = html.find(">", i)
                resp.set_data(html[:j + 1] + TRIAL_BANNER + html[j + 1:])
    except Exception as exc:
        logger.warning("trial banner: %s", exc)
    return resp


def init_db():
    with app.app_context():
        try:
            db.create_all()
            # Run migrations for existing DBs
            run_migrations()
            # Backstop: add anything the migrations above still do not cover.
            reconcile_schema()
            # Keep the tables off Supabase's public Data API.
            secure_supabase_exposure()
            if not Employee.query.first():
                _admin_pw = os.environ.get("ADMIN_PASSWORD", "admin123")
                if _admin_pw == "admin123":
                    logger.warning("Using default admin password 'admin123'. Set ADMIN_PASSWORD env var for production.")
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
                logger.info("Database initialized successfully")
            seed_trial_demo()
            # Seed default auto badges if none exist
            try:
                if Badge.query.count() == 0:
                    default_badges = [
                        Badge(name="Perfect Week", description="100% accuracy for 5+ consecutive days", icon="", badge_type="auto"),
                        Badge(name="Speed Demon", description="Pick speed > 60 items/hr average this month", icon="", badge_type="auto"),
                        Badge(name="Clean Sweep", description="100% workspace score for full week", icon="", badge_type="auto"),
                        Badge(name="Century Club", description="100+ items picked in a single day", icon="", badge_type="auto"),
                        Badge(name="Top Performer", description="#1 score in role for the month", icon="", badge_type="auto"),
                        Badge(name="7-Day Streak", description="Submitted every day for 7 consecutive days", icon="", badge_type="auto"),
                        Badge(name="First Delivery", description="Completed your very first GPS delivery", icon="", badge_type="auto"),
                        Badge(name="Speed Rider", description="Completed 3 deliveries faster than the average time", icon="", badge_type="auto"),
                        Badge(name="Perfect Courier", description="10 consecutive on-time deliveries", icon="", badge_type="auto"),
                        Badge(name="Veteran Courier", description="50 total deliveries completed", icon="", badge_type="auto"),
                    ]
                    for b in default_badges:
                        db.session.add(b)
                    db.session.commit()
                    logger.info("Default badges seeded")
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

            # Brute-force lockout: 5 wrong tries per account → 10 min lock
            _lk = f"login:{email}"
            _wait = _throttle_check(_lk)
            if _wait:
                flash(f"Too many failed attempts. Try again in {max(_wait // 60, 1)} minute(s).", "danger")
                return render_template("login.html")

            user = Employee.query.filter_by(email=email).first()

            if not user or not user.check_password(password):
                _throttle_fail(_lk)
                logger.warning(f"Failed login attempt for: {redact_email(email)} (ip={request.remote_addr})")
                flash("That email and password don't match. Please try again.", "danger")
                return render_template("login.html")
            _throttle_clear(_lk)

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

            logger.info(f"User login successful: {redact_email(email)}")
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
    """Save (or update) the staff member's multitask reports for the day.
    A multitasker can pick MANY extra roles (checkboxes → multitask_roles list);
    the legacy single `multitask_role` is still honoured. Each role's own KPI
    params are read from its mt_* fields and upserted on (emp, date, role) so the
    chosen roles can differ each day. No-ops cleanly when nothing was entered."""
    roles = list(request.form.getlist("multitask_roles"))
    legacy = (request.form.get("multitask_role", "") or "").strip()
    if legacy:
        roles.append(legacy)

    # SAFETY NET: a staff member can fill a role's numbers but forget to tick its
    # checkbox (or the JS toggle misfires). Auto-include any role that has at least
    # one number entered, so nothing the user typed is ever silently dropped.
    role_fields = {
        "picker":    ("mt_p_total_bills", "mt_p_sbo", "mt_p_picked", "mt_p_missed", "mt_p_cso", "mt_p_packing"),
        "checker":   ("mt_c_bills_received", "mt_c_pending", "mt_c_sbo", "mt_c_cso", "mt_c_checked", "mt_c_errors"),
        "purchaser": ("mt_pu_sbo", "mt_pu_checked", "mt_pu_picked", "mt_pu_items", "mt_pu_cso", "mt_pu_packing"),
        "billing":   ("mt_b_bills", "mt_b_items", "mt_b_quantity"),
        "other":     ("mt_o_breakage", "mt_o_expire", "mt_o_processed", "mt_o_recv_qty", "mt_o_exp_return"),
    }
    def _has_value(field):
        try: return int(request.form.get(field, 0) or 0) > 0
        except Exception: return False
    for role, fields in role_fields.items():
        if any(_has_value(f) for f in fields):
            roles.append(role)
    # delivery / packing per-role quantity fields (mt_s_quantity__<role>)
    for key in request.form:
        if key.startswith("mt_s_quantity__") and _has_value(key):
            roles.append(key[len("mt_s_quantity__"):].strip())

    # Dedupe, drop blanks + the primary role
    seen = set()
    roles = [r for r in (x.strip() for x in roles)
             if r and r != primary_role and not (r in seen or seen.add(r))]
    if not roles:
        return 0

    def mi(k):
        try: return max(0, int(request.form.get(k, 0) or 0))
        except Exception: return 0
    note = (request.form.get("multitask_note", "") or "").strip()[:300]

    saved = 0
    for role in roles:
        vals = dict(sales_bills_open=0, picked=0, missed=0, checked=0, errors_found=0,
                    packing_done=0, cs_sales_open=0, total_bills_received=0,
                    bills_received=0, pending_bills_manual=0, quantity=0,
                    bills_done=0, items_count=0,
                    breakage_received=0, expire_received=0, breakage_expire_processed=0,
                    item_receive_qty=0, expire_return_qty=0)
        if role == "picker":
            vals.update(total_bills_received=mi("mt_p_total_bills"),
                        sales_bills_open=mi("mt_p_sbo"), picked=mi("mt_p_picked"),
                        missed=mi("mt_p_missed"), cs_sales_open=mi("mt_p_cso"),
                        packing_done=mi("mt_p_packing"))
        elif role == "checker":
            vals.update(bills_received=mi("mt_c_bills_received"),
                        sales_bills_open=mi("mt_c_sbo"), cs_sales_open=mi("mt_c_cso"),
                        checked=mi("mt_c_checked"), errors_found=mi("mt_c_errors"))
        elif role == "purchaser":
            vals.update(sales_bills_open=mi("mt_pu_sbo"), checked=mi("mt_pu_checked"),
                        picked=mi("mt_pu_picked"), errors_found=mi("mt_pu_items"),
                        cs_sales_open=mi("mt_pu_cso"), packing_done=mi("mt_pu_packing"))
        elif role == "billing":
            vals.update(bills_done=mi("mt_b_bills"), items_count=mi("mt_b_items"),
                        quantity=mi("mt_b_quantity"))
        elif role == "other":
            vals.update(breakage_received=mi("mt_o_breakage"),
                        expire_received=mi("mt_o_expire"),
                        breakage_expire_processed=mi("mt_o_processed"),
                        item_receive_qty=mi("mt_o_recv_qty"),
                        expire_return_qty=mi("mt_o_exp_return"))
        else:  # delivery / packing — per-role quantity, legacy fallback
            qty = mi(f"mt_s_quantity__{role}") or mi("mt_s_quantity")
            vals.update(quantity=qty)

        # Skip a role with nothing filled in
        if not any(vals.values()) and not note:
            continue

        _ep = entry_problems(role, vals) if role in ("picker", "checker", "purchaser") else []
        if _ep:
            flash("%s figures not saved — %s" % (role.title(), " ".join(_ep)), "danger")
            continue

        # Checking as a second role still clears bills off the same pile, so the
        # same ceiling applies: normal + urgent cannot exceed the day's total.
        if role == "checker":
            _bp = checker_bill_problems(entry_date, emp_id, vals["bills_received"],
                                        vals["sales_bills_open"], vals["cs_sales_open"])
            if _bp:
                flash("Multitask checker figures not saved — " + " ".join(_bp), "danger")
                continue
            for _w in checker_bill_warnings(entry_date, emp_id, vals["bills_received"],
                                            vals["sales_bills_open"], vals["cs_sales_open"]):
                flash(_w, "warning")

        mt = MultitaskEntry.query.filter_by(
            emp_id=emp_id, entry_date=entry_date, secondary_type=role
        ).first()
        if not mt:
            mt = MultitaskEntry(emp_id=emp_id, entry_date=entry_date, secondary_type=role)
            db.session.add(mt)
        for k, v in vals.items():
            setattr(mt, k, v)
        mt.note = note or None
        saved += 1
    db.session.commit()
    invalidate_request_memos()
    return saved


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    try:
        emp_id = session.get("user_id")
        staff_type = session.get("staff_type", "picker")
        today = date.today()

        # Supervisor has a dedicated bill-validation console, not the KPI dashboard
        if staff_type == "supervisor" and not session.get("is_admin"):
            return redirect(url_for("supervisor_console"))

        today_entry = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=today).first()
        new_personal_best = False
        g.assigned_day_total = pick_day_total(today)

        # ── Delivery / Assigner / Packer: simple note entry (no KPIEntry) ───────
        today_db_note = None
        if staff_type in ("delivery", "biller", "packer"):
            # Independent multitask submission — works for delivery/biller/packer too
            if request.method == "POST" and request.form.get("multitask_only") == "1":
                try:
                    _save_multitask_entry(emp_id, today, staff_type)
                    flash("Multitask role report saved.", "success")
                except Exception as mte:
                    db.session.rollback()
                    logger.warning(f"Independent multitask save (note path): {mte}")
                    flash("Error saving multitask report.", "danger")
                return redirect(url_for("dashboard"))

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
                            table_clean=1 if request.form.get("table_clean") == "1" else 0,
                            sweep_done=1 if request.form.get("sweep_done") == "1" else 0,
                        )
                        db.session.add(db_note)
                        db.session.commit()
                        flash("Daily note recorded successfully.", "success")
                        for _ev, _msg in evaluate_levels(db.session.get(Employee, emp_id), day=today):
                            if _msg:
                                flash("%s — %s" % _msg, "success")
                        return redirect(url_for("dashboard"))
                    except Exception as dbe:
                        db.session.rollback()
                        logger.error(f"DeliveryBillerNote save: {dbe}")
                        flash("Error saving note.", "danger")

            # ── Delivery module data (assignments for delivery staff, staff list for biller) ──
            my_assignments = []
            purchaser_delivery_staff = []
            my_deliveries_today = []
            trip_map = {}
            delivery_diag = None
            stops_map = {}
            my_dispatches = []
            assigner_today = None
            try:
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
                    if my_assignments:
                        aid_list = [a.id for a in my_assignments]
                        trips = DeliveryTrip.query.filter(DeliveryTrip.assignment_id.in_(aid_list)).all()
                        trip_map = {t.assignment_id: t for t in trips}
                        stops = DeliveryStop.query.filter(DeliveryStop.assignment_id.in_(aid_list)).order_by(DeliveryStop.id).all()
                        for st in stops:
                            stops_map.setdefault(st.assignment_id, []).append(st)
                elif staff_type == "biller":
                    all_emps = Employee.query.all()
                    _role_breakdown = {}
                    for _e in all_emps:
                        if getattr(_e, "is_admin", False):
                            continue
                        primary = (getattr(_e, "staff_type", "") or "").lower()
                        secondary = (getattr(_e, "secondary_staff_type", "") or "").lower()
                        _role_breakdown[primary] = _role_breakdown.get(primary, 0) + 1
                        if primary == "delivery" or secondary == "delivery":
                            purchaser_delivery_staff.append(_e)
                    delivery_diag = {
                        "total_emps": len(all_emps),
                        "del_total": len(purchaser_delivery_staff),
                        "breakdown": _role_breakdown,
                    }
                    # Assigner's recent dispatches (so they can edit/delete)
                    my_dispatches = (DeliveryAssignment.query
                                     .filter(DeliveryAssignment.purchaser_id == emp_id,
                                             DeliveryAssignment.status.in_(["pending", "in_transit"]))
                                     .order_by(DeliveryAssignment.assigned_at.desc()).limit(30).all())
                    if my_dispatches:
                        aid_list = [a.id for a in my_dispatches]
                        stops = DeliveryStop.query.filter(DeliveryStop.assignment_id.in_(aid_list)).order_by(DeliveryStop.id).all()
                        for st in stops:
                            stops_map.setdefault(st.assignment_id, []).append(st)
                    # End-of-day summary: everything THIS assigner dispatched today
                    assigner_today = _assigner_day_summary(emp_id, today, emp_map={e.id: e for e in all_emps})
                    logger.info(f"Biller dashboard (note path): {len(purchaser_delivery_staff)} delivery staff "
                                f"of {len(all_emps)} employees, breakdown={_role_breakdown}")
            except Exception as _de:
                logger.error(f"Delivery module (note path) failed: {_de}")

            # Past store names (so the assigner gets autocomplete suggestions)
            try:
                known_stores = [r[0] for r in db.session.query(DeliveryStop.place_name)
                                .distinct().order_by(DeliveryStop.place_name).all() if r[0]]
            except Exception:
                known_stores = []
            # Multitask entries logged today (so delivery/biller can multitask too)
            try:
                note_today_multitask = MultitaskEntry.query.filter_by(
                    emp_id=emp_id, entry_date=today
                ).order_by(MultitaskEntry.created_at.desc()).all()
            except Exception:
                note_today_multitask = []

            # Inline delivery leaderboard (delivery staff only on this path)
            note_delivery_lb = []
            if staff_type == "delivery":
                try:
                    _month_start = today.replace(day=1)
                    _dl_staff = Employee.query.filter(
                        Employee.is_admin == False,
                        or_(Employee.staff_type == "delivery",
                            Employee.secondary_staff_type == "delivery")
                    ).all()
                    for _de in _dl_staff:
                        _all_trips = DeliveryTrip.query.filter_by(emp_id=_de.id, status="completed").all()
                        _mtrips = [t for t in _all_trips if t.trip_date and t.trip_date >= _month_start]
                        _mscore = sum(score_delivery_trip(t) for t in _mtrips)
                        _total = len(_all_trips)
                        _on_t = sum(1 for t in _all_trips if t.is_on_time)
                        note_delivery_lb.append({
                            "name": _de.name,
                            "is_self": _de.id == emp_id,
                            "month_trips": len(_mtrips),
                            "month_score": _mscore,
                            "total": _total,
                            "on_time_pct": round(_on_t / max(_total, 1) * 100),
                        })
                    note_delivery_lb.sort(key=lambda x: x["month_score"], reverse=True)
                except Exception as _lbe2:
                    logger.warning(f"note_delivery_lb: {_lbe2}")

            return render_template("dashboard.html",
                user_name=session.get("user_name", "User"),
                user_id=emp_id,
                staff_type=staff_type,
                today=today,
                today_entry=None,
                my_checklist=checklist_for(emp_id, staff_type, today),
                today_db_note=today_db_note,
                d_stats=None, w_stats=None, m_stats=None, monthly_target=None,
                recent=DeliveryBillerNote.query.filter_by(emp_id=emp_id).order_by(
                    DeliveryBillerNote.entry_date.desc()).limit(14).all(),
                trend_labels=[], trend_accuracy=[], trend_speed=[],
                total_entries=DeliveryBillerNote.query.filter_by(emp_id=emp_id).count(),
                new_personal_best=False, pending_windows=[], active_announcements=[],
                unread_count=0, current_goal=None, pending_requests=[],
                my_staff_badges=[], all_auto_badges=[], earned_badge_ids=set(),
                current_month=today.strftime("%B %Y"), month_str=today.strftime("%Y-%m"),
                today_multitask=note_today_multitask,
                known_stores=known_stores,
                primary_staff_type=session.get("primary_staff_type") or staff_type,
                secondary_staff_type=session.get("secondary_staff_type"),
                my_assignments=my_assignments,
                purchaser_delivery_staff=purchaser_delivery_staff,
                my_deliveries_today=my_deliveries_today,
                trip_map=trip_map,
                delivery_diag=delivery_diag,
                stops_map=stops_map,
                my_dispatches=my_dispatches,
                assigner_today=assigner_today,
                known_routes=KNOWN_ROUTES,
                packet_types=PACKET_TYPES,
                store_lat=STORE_LAT,
                store_lng=STORE_LNG,
                tomtom_key=TOMTOM_API_KEY,
                delivery_lb=note_delivery_lb,
            )

        # ── Independent multitask submission (Role 2 can be filled/changed any time) ──
        if request.method == "POST" and request.form.get("multitask_only") == "1":
            try:
                _save_multitask_entry(emp_id, today, staff_type)
                flash("Multitask role report saved.", "success")
            except Exception as mte:
                db.session.rollback()
                logger.warning(f"Independent multitask save: {mte}")
                flash("Error saving multitask report.", "danger")
            return redirect(url_for("dashboard"))

        # Pickers no longer type daily totals: their work is recorded bill by
        # bill as they finish grabs. The Pick Assigner has no daily form.
        if request.method == "POST" and staff_type in ("picker", "pick_assigner"):
            flash("Picking is recorded live, bill by bill. There is no daily form to submit.", "info")
            return redirect(url_for("dashboard"))
        if request.method == "POST" and not today_entry:
            is_sunday = today.weekday() == 6
            emp = db.session.get(Employee, emp_id)
            if is_sunday and not (emp and emp.sunday_override):
                flash("Sunday is a holiday. No data submission allowed.", "warning")
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
                        if checked + errors_found <= 0:
                            missing_fields.append("Items Checked (normal or urgent)")
                    elif staff_type == "purchaser":
                        if sales_bills_open <= 0:
                            missing_fields.append("PO Bills Received (TB Sales)")
                        if errors_found <= 0:
                            missing_fields.append("Number of Items")

                    if missing_fields:
                        flash(f"Cannot save — required fields missing or zero: {', '.join(missing_fields)}.", "danger")
                        return redirect(url_for("dashboard"))

                    _ep = entry_problems(staff_type, dict(
                        total_bills_received=total_bills_received, sales_bills_open=sales_bills_open,
                        picked=picked, missed=missed, cs_sales_open=cs_sales_open, checked=checked,
                        errors_found=errors_found, packing_done=packing_done,
                        rack_organized=gi("rack_organized")))
                    if _ep:
                        flash("Cannot save — " + " ".join(_ep), "danger")
                        return redirect(url_for("dashboard"))

                    if staff_type == "checker":
                        _bp = checker_bill_problems(today, emp_id, bills_received,
                                                    sales_bills_open, cs_sales_open)
                        if _bp:
                            flash("Cannot save — " + " ".join(_bp), "danger")
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
                    # The team pending pile just changed.
                    invalidate_request_memos()
                    for _ev, _msg in evaluate_levels(db.session.get(Employee, emp_id), day=today):
                        if _msg:
                            flash("%s — %s" % _msg, "success")
                    if staff_type == "checker":
                        for _w in checker_bill_warnings(today, emp_id, bills_received,
                                                        sales_bills_open, cs_sales_open):
                            flash(_w, "warning")

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

                    flash("Metrics recorded successfully.", "success")
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
        monthly_target = compute_monthly_target(emp_id, staff_type)

        trend_labels = []
        trend_accuracy = []
        trend_speed = []
        # Each role's own two measures. This read picker fields for everyone,
        # so a checker's or purchaser's trend sat at 0% every day.
        _tdays = (checker_team_day_totals([today - timedelta(days=i) for i in range(7)])
                  if staff_type == "checker" else {})
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            e = next((x for x in all_entries if x.entry_date == d), None)
            trend_labels.append(d.strftime("%a %d"))
            if e:
                if staff_type == "checker":
                    _t = _tdays.get(d) or {}
                    acc = round(min(_t.get("checked", 0) / _t["total"] * 100, 100), 1) if _t.get("total") else None
                    spd = round(((e.checked or 0) + (e.errors_found or 0) * URGENT_BILL_WEIGHT) / 9.0, 1)
                elif staff_type == "purchaser":
                    rec = e.sales_bills_open or 0
                    acc = round(min((e.checked or 0) / rec * 100, 100), 1) if rec else None
                    spd = round((e.errors_found or 0) / 9.0, 1)
                else:
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

        # Past store names for assigner autocomplete (used by biller/admin dispatch form)
        try:
            known_stores_main = [r[0] for r in db.session.query(DeliveryStop.place_name)
                                 .distinct().order_by(DeliveryStop.place_name).all() if r[0]]
        except Exception:
            known_stores_main = []

        # The multitask bonus is applied inside build_analytics, so it already reaches
        # d_stats, w_stats, m_stats, the admin view, the leaderboards and the reports.

        # Delivery leaderboard (inline, delivery staff only)
        delivery_lb = []
        try:
            if staff_type == "delivery":
                _today = today
                _month_start = _today.replace(day=1)
                _dl_staff = Employee.query.filter(
                    Employee.is_admin == False,
                    or_(Employee.staff_type == "delivery",
                        Employee.secondary_staff_type == "delivery")
                ).all()
                for _de in _dl_staff:
                    _all_trips = DeliveryTrip.query.filter_by(emp_id=_de.id, status="completed").all()
                    _mtrips = [t for t in _all_trips if t.trip_date and t.trip_date >= _month_start]
                    _mscore = sum(score_delivery_trip(t) for t in _mtrips)
                    _total = len(_all_trips)
                    _on_t = sum(1 for t in _all_trips if t.is_on_time)
                    delivery_lb.append({
                        "name": _de.name,
                        "is_self": _de.id == emp_id,
                        "month_trips": len(_mtrips),
                        "month_score": _mscore,
                        "total": _total,
                        "on_time_pct": round(_on_t / max(_total, 1) * 100),
                    })
                delivery_lb.sort(key=lambda x: x["month_score"], reverse=True)
        except Exception as _lbe:
            logger.warning(f"delivery_lb build: {_lbe}")
            delivery_lb = []

        # Delivery module data
        delivery_diag = None
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
                # Filter in Python to avoid NULL/ORM edge cases (is_admin, missing secondary col)
                all_emps = Employee.query.all()
                purchaser_delivery_staff = []
                _role_breakdown = {}
                for _e in all_emps:
                    if getattr(_e, "is_admin", False):
                        continue
                    primary = (getattr(_e, "staff_type", "") or "").lower()
                    secondary = (getattr(_e, "secondary_staff_type", "") or "").lower()
                    _role_breakdown[primary] = _role_breakdown.get(primary, 0) + 1
                    if primary == "delivery" or secondary == "delivery":
                        purchaser_delivery_staff.append(_e)
                _emp_total = len(all_emps)
                _del_total = len(purchaser_delivery_staff)
                logger.info(f"Biller dashboard: found {_del_total} delivery staff "
                            f"(scanned {_emp_total} employees, breakdown={_role_breakdown})")
                delivery_diag = {
                    "total_emps": _emp_total,
                    "del_total": _del_total,
                    "breakdown": _role_breakdown,
                }
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
            levels=level_summary(db.session.get(Employee, emp_id)),
            checker_bests=(checker_month_bests(emp_id)
                           if staff_type == "checker" or session.get("secondary_staff_type") == "checker"
                           else None),
            user_name=session.get("user_name", "User"),
            user_id=emp_id,
            staff_type=staff_type,
            today=today,
            today_entry=today_entry,
            d_stats=d_stats,
            w_stats=w_stats,
            m_stats=m_stats,
            monthly_target=monthly_target,
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
            my_checklist=checklist_for(emp_id, staff_type, today),
            today_db_note=None,
            primary_staff_type=session.get("primary_staff_type") or staff_type,
            secondary_staff_type=session.get("secondary_staff_type"),
            my_assignments=my_assignments,
            purchaser_delivery_staff=purchaser_delivery_staff,
            my_deliveries_today=my_deliveries_today,
            trip_map=trip_map,
            stops_map={},
            my_dispatches=[],
            store_lat=STORE_LAT,
            store_lng=STORE_LNG,
            tomtom_key=TOMTOM_API_KEY,
            delivery_diag=delivery_diag,
            assigner_today=None,
            known_routes=KNOWN_ROUTES,
            packet_types=PACKET_TYPES,
            known_stores=known_stores_main,
            delivery_lb=delivery_lb,
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return render_template("dashboard.html",
            user_name="User", user_id=0, staff_type="picker", today=date.today(),
            today_entry=None, d_stats=None, w_stats=None, m_stats=None, monthly_target=None,
            recent=[], trend_labels=[], trend_accuracy=[], trend_speed=[], total_entries=0,
            new_personal_best=False, pending_windows=[],
            active_announcements=[], unread_count=0, current_goal=None,
            pending_requests=[], my_staff_badges=[], all_auto_badges=[],
            earned_badge_ids=set(), current_month="", month_str="",
            today_multitask=[], today_db_note=None, my_checklist=[],
            primary_staff_type="picker", secondary_staff_type=None,
            my_assignments=[], purchaser_delivery_staff=[], my_deliveries_today=[],
            trip_map={}, stops_map={}, my_dispatches=[],
            store_lat=0.0, store_lng=0.0, tomtom_key="", delivery_diag=None,
            assigner_today=None, known_routes=[], packet_types=[], known_stores=[],
            delivery_lb=[])


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
            day_ents = [e for e in ents if e.entry_date == today]
            day_stats = build_analytics(day_ents, emp.staff_type, emp_id=emp.id) if day_ents else None
            rows.append({
                'emp': emp,
                'stats': stats,
                'week_stats': week_stats,
                'day_stats': day_stats,
                'count': len(ents),
                'last_entry': max((e.entry_date for e in ents), default=None),
            })

        # Summed by the database. This loaded every entry ever made into memory
        # on every visit -- a cost that grew every single day.
        from sqlalchemy import func as _f
        total_picked, total_entries = db.session.query(
            _f.coalesce(_f.sum(KPIEntry.picked), 0), _f.count(KPIEntry.id)).one()
        total_picked, total_entries = int(total_picked or 0), int(total_entries or 0)
        active_today = KPIEntry.query.filter_by(entry_date=today).count()
        open_windows = PastEntryWindow.query.filter_by(is_active=True).order_by(PastEntryWindow.past_date.desc()).all()
        # Last 12 months (incl. current) for the bulk open/close month picker
        selectable_months = []
        _y, _m = today.year, today.month
        for _ in range(12):
            selectable_months.append({"value": f"{_y:04d}-{_m:02d}",
                                      "label": date(_y, _m, 1).strftime("%B %Y")})
            _m -= 1
            if _m == 0:
                _m = 12; _y -= 1
        open_complaints = Complaint.query.filter_by(is_resolved=False).order_by(Complaint.created_at.desc()).all()

        stale_validations_count = 0   # Bill Validations feature removed

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
        except Exception as _mte:
            logger.error(f"recent_multitask query failed (check multitask_entries columns/migration): {_mte}")
            recent_multitask = []

        # Feature 5: Badges for admin
        try:
            all_badges = Badge.query.all()
        except Exception:
            all_badges = []

        try:
            mt_report = multitask_report(30)
        except Exception as _mte2:
            logger.error(f"admin mt_report: {_mte2}")
            mt_report = {"by_role": {}, "staff": []}
        try:
            recent_levelups = [(ev, db.session.get(Employee, ev.emp_id)) for ev in
                               LevelEvent.query.filter(LevelEvent.created_at >= datetime.utcnow() - timedelta(days=7))
                               .order_by(LevelEvent.created_at.desc()).limit(20).all()]
        except Exception:
            recent_levelups = []
        level_status = level_status_all()
        return render_template("admin.html",
            recent_levelups=recent_levelups, level_status=level_status,
            mt_report=mt_report, role_order=MULTITASK_ROLE_ORDER,
            role_label=multitask_role_label,
            rows=rows, total_picked=total_picked, total_entries=total_entries,
            active_today=active_today, emp_count=len(employees), today=today,
            current_month=today.strftime("%B %Y"),
            open_windows=open_windows,
            selectable_months=selectable_months,
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
                               open_windows=[], selectable_months=[], open_complaints=[], all_staff=[],
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
            # Every colleague's entries in one query, not one query each.
            _ents_by_id = {}
            for _e in KPIEntry.query.filter(KPIEntry.emp_id.in_([x.id for x in all_staff])).all():
                _ents_by_id.setdefault(_e.emp_id, []).append(_e)
            for s in all_staff:
                s_stats = build_analytics(_ents_by_id.get(s.id, []), s.staff_type, emp_id=s.id)
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

        # Daily notes (delivery / assigner / packer staff log these instead of KPI entries)
        try:
            daily_notes = (DeliveryBillerNote.query.filter_by(emp_id=emp_id)
                           .order_by(DeliveryBillerNote.entry_date.desc()).limit(30).all())
        except Exception:
            daily_notes = []

        # Delivery trips for delivery staff — what they actually delivered
        try:
            staff_trips = (DeliveryTrip.query.filter_by(emp_id=emp_id, status="completed")
                           .order_by(DeliveryTrip.trip_date.desc()).limit(30).all())
        except Exception:
            staff_trips = []

        return render_template("staff_detail.html",
            checker_bests=(checker_month_bests(emp.id)
                           if "checker" in (emp.staff_type, emp.secondary_staff_type) else None),
            mt_days=multitask_days_for(emp.id),
            levels=level_summary(emp),
            # The suggestion the "Apply" button will actually apply (last 30 days).
            target_sug=(build_analytics(get_period_entries(emp.id, "last30"), emp.staff_type,
                                        emp_id=emp.id) or {}).get("target_suggestion"),
            team_days=(checker_team_day_totals({e.entry_date for e in entries})
                       if emp.staff_type == "checker" else {}),
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
            daily_notes=daily_notes,
            staff_trips=staff_trips,
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
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return redirect(url_for("admin_dashboard"))
        if Employee.query.filter_by(email=email).first():
            flash(f"Email '{email}' already exists.", "warning")
            return redirect(url_for("admin_dashboard"))

        if staff_type not in VALID_STAFF_TYPES:
            staff_type = "picker"
        emp = Employee(name=name, email=email, staff_type=staff_type,
                       role=ROLE_DISPLAY_NAMES.get(staff_type, f"Operations {staff_type.title()}"))
        emp.set_password(password)
        db.session.add(emp)
        db.session.commit()
        log_audit("add_user", name, f"Added as {staff_type}")
        flash(f"{name} added successfully.", "success")
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
        if staff_type in VALID_STAFF_TYPES:
            emp.staff_type = staff_type
            emp.role = ROLE_DISPLAY_NAMES.get(staff_type, f"Operations {staff_type.title()}")
        # Secondary role (optional; "" = clear)
        sec_raw = request.form.get("secondary_staff_type", None)
        if sec_raw is not None:
            sec = sec_raw.strip()
            if sec == "" or sec == "none":
                emp.secondary_staff_type = None
            elif sec in VALID_STAFF_TYPES and sec != emp.staff_type:
                emp.secondary_staff_type = sec
        if new_password:
            if len(new_password) < 8:
                flash("New password must be at least 8 characters.", "danger")
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
        flash(f"{emp.name} updated successfully.", "success")
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
        if not emp:
            flash("User not found.", "warning")
            return redirect(url_for("admin_dashboard"))
        if emp.is_admin:
            flash("Admin accounts cannot be deleted.", "warning")
            return redirect(url_for("admin_dashboard"))

        name = emp.name
        # Explicitly clear dependent rows whose FK has no ON DELETE CASCADE,
        # so the delete never fails on a constraint.
        try:
            DeliveryStop.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            DeliveryTrip.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            DeliveryAssignment.query.filter(
                (DeliveryAssignment.delivery_emp_id == emp_id) |
                (DeliveryAssignment.purchaser_id == emp_id)
            ).delete(synchronize_session=False)
            DeliveryBillerNote.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            MultitaskEntry.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            PersonalRecord.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            LevelEvent.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            Notification.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            PickMissing.query.filter_by(picker_id=emp_id).delete(synchronize_session=False)
            PickEvent.query.filter_by(picker_id=emp_id).delete(synchronize_session=False)
            PushSub.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            PickClaim.query.filter_by(picker_id=emp_id).delete(synchronize_session=False)
            PickBill.query.filter_by(created_by=emp_id).update({"created_by": None}, synchronize_session=False)
            SupervisorBill.query.filter(
                (SupervisorBill.staff_id == emp_id) |
                (SupervisorBill.supervisor_id == emp_id)
            ).delete(synchronize_session=False)
            KPIEntry.query.filter_by(emp_id=emp_id).delete(synchronize_session=False)
            # Nullable references to this employee from rows we must KEEP.
            # These FKs have no ON DELETE CASCADE, so on PostgreSQL/Supabase the
            # delete below would abort with a constraint violation. (SQLite does
            # not enforce foreign keys by default, which is why this only ever
            # showed up in production.)
            BillValidation.query.filter_by(checker1_id=emp_id).update(
                {"checker1_id": None}, synchronize_session=False)
            BillValidation.query.filter_by(checker2_id=emp_id).update(
                {"checker2_id": None}, synchronize_session=False)
            BillValidation.query.filter_by(checker3_id=emp_id).update(
                {"checker3_id": None}, synchronize_session=False)
            BillValidation.query.filter_by(override_by=emp_id).update(
                {"override_by": None}, synchronize_session=False)
            Complaint.query.filter_by(target_emp_id=emp_id).update(
                {"target_emp_id": None}, synchronize_session=False)
            Complaint.query.filter_by(reported_by=emp_id).update(
                {"reported_by": None}, synchronize_session=False)
            StaffBadge.query.filter_by(awarded_by=emp_id).update(
                {"awarded_by": None}, synchronize_session=False)
            PastEntryWindow.query.filter_by(opened_by=emp_id).update(
                {"opened_by": None}, synchronize_session=False)
            Announcement.query.filter_by(created_by=emp_id).update(
                {"created_by": None}, synchronize_session=False)
            AuditLog.query.filter_by(admin_id=emp_id).update(
                {"admin_id": None}, synchronize_session=False)
            # AdminStaffNote.created_by is NOT NULL, so those rows go with the author.
            AdminStaffNote.query.filter_by(created_by=emp_id).delete(synchronize_session=False)
        except Exception as dep_err:
            logger.warning(f"delete_user dependent cleanup: {dep_err}")

        db.session.delete(emp)
        db.session.commit()
        log_audit("delete_user", name, "User removed")
        flash(f"{name} removed.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_user: {e}")
        flash(f"Error deleting user: {e}", "danger")
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
                flash(f"Entry window for {past_date.strftime('%d %b %Y')} closed.", "success")
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
            flash(f"Past entry window opened for {past_date.strftime('%d %b %Y')}. "
                  f"Staff who haven't submitted can now enter their data.", "success")

    except ValueError:
        flash("Invalid date format.", "danger")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_open_past_window: {e}")
        flash("Error updating entry window.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/past_window_bulk", methods=["POST"])
@admin_required
def admin_past_window_bulk():
    """Open or close past-entry windows for whole month(s) in a single click.
    Accepts `months` (one or more YYYY-MM values) and an `action` (open|close)."""
    try:
        action = request.form.get("action", "open")
        months = request.form.getlist("months") or request.form.getlist("months[]")
        # Also accept a single 'month' field for convenience
        single = (request.form.get("month", "") or "").strip()
        if single:
            months.append(single)
        months = [m.strip() for m in months if m and m.strip()]
        if not months and action == "close":
            # "Close All Days" with no month picked means exactly that: close
            # every window that is open. It used to refuse with "Pick at least
            # one month", which read as the button not working -- especially
            # on a phone, where picking from a multi-select list is awkward.
            n = (PastEntryWindow.query.filter(PastEntryWindow.is_active.is_(True))
                 .update({"is_active": False}, synchronize_session=False))
            db.session.commit()
            log_audit("bulk_close_past_windows", "all", f"{n} days closed")
            flash(f"Closed all {n} open past-entry day(s)." if n else "No past-entry days were open.",
                  "success" if n else "info")
            return redirect(url_for("admin_dashboard") + "#admin")
        if not months:
            flash("Pick at least one month to open.", "warning")
            return redirect(url_for("admin_dashboard") + "#admin")

        today = date.today()
        opened = closed = 0
        # Every past day in the chosen months, gathered first. This used to ask
        # the database about each day separately -- six months was ~180 round
        # trips to Supabase, which is why "Close All Days" took seconds. Now it
        # is one query for the lot.
        all_days = []
        for ym in months:
            try:
                year, mon = (int(x) for x in ym.split("-")[:2])
            except (ValueError, IndexError):
                continue
            d = date(year, mon, 1)
            while d.month == mon and d.year == year:
                if d < today:
                    all_days.append(d)
                d += timedelta(days=1)
        existing = {w.past_date: w for w in
                    PastEntryWindow.query.filter(PastEntryWindow.past_date.in_(all_days)).all()} \
            if all_days else {}
        new_rows = []
        for d in all_days:
                    window = existing.get(d)
                    if action == "close":
                        if window and window.is_active:
                            window.is_active = False
                            closed += 1
                    else:
                        if window:
                            if not window.is_active:
                                window.is_active = True
                                window.opened_by = session.get("user_id")
                                window.opened_at = datetime.utcnow()
                                opened += 1
                        else:
                            new_rows.append({"past_date": d, "opened_by": session.get("user_id"),
                                             "opened_at": datetime.utcnow(), "is_active": True})
                            opened += 1
        if new_rows:
            db.session.execute(PastEntryWindow.__table__.insert(), new_rows)   # one statement
        db.session.commit()

        if action == "close":
            log_audit("bulk_close_past_windows", ", ".join(months), f"{closed} days closed")
            flash(f"Closed {closed} past-entry day(s) across {len(months)} month(s).", "success")
        else:
            log_audit("bulk_open_past_windows", ", ".join(months), f"{opened} days opened")
            flash(f"Opened {opened} past-entry day(s) across {len(months)} month(s). "
                  f"Staff who haven't submitted can now backfill any day in those months.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_past_window_bulk: {e}")
        flash("Error updating month windows.", "danger")
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

    if staff_type in ("picker", "pick_assigner"):
        flash("Picking is recorded live, bill by bill, so there is nothing to enter for a past date.", "info")
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
                if checked + errors_found <= 0:
                    missing_fields.append("Items Checked (normal or urgent)")
            elif staff_type == "purchaser":
                if sales_bills_open <= 0:
                    missing_fields.append("PO Bills Received (TB Sales)")
                if errors_found <= 0:
                    missing_fields.append("Number of Items")

            if missing_fields:
                flash(f"Cannot save — required fields missing or zero: {', '.join(missing_fields)}.", "danger")
                return redirect(url_for("past_entry", date_str=date_str))

            _ep = entry_problems(staff_type, dict(
                total_bills_received=total_bills_received, sales_bills_open=sales_bills_open,
                picked=picked, missed=missed, cs_sales_open=cs_sales_open, checked=checked,
                errors_found=errors_found, packing_done=packing_done, rack_organized=rack_organized))
            if _ep:
                flash("Cannot save — " + " ".join(_ep), "danger")
                return redirect(url_for("past_entry", date_str=date_str))

            if staff_type == "checker":
                _bp = checker_bill_problems(past_date, emp_id, bills_received,
                                            sales_bills_open, cs_sales_open)
                if _bp:
                    flash("Cannot save — " + " ".join(_bp), "danger")
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
            invalidate_request_memos()
            for _ev, _msg in evaluate_levels(db.session.get(Employee, emp_id), day=past_date):
                if _msg:
                    flash("%s — %s" % _msg, "success")
            if staff_type == "checker":
                for _w in checker_bill_warnings(past_date, emp_id, bills_received,
                                                sales_bills_open, cs_sales_open):
                    flash(_w, "warning")
            log_audit("past_entry_submit", session.get("user_name", ""), f"Submitted past data for {past_date}")
            flash(f"Past entry for {past_date.strftime('%d %b %Y')} saved successfully.", "success")
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
    # Abandon whatever the crashed request had half-written first: it must not
    # be saved, and while it is open it can block the error log from writing.
    try:
        db.session.rollback()
    except Exception:
        pass
    logger.error(f"500: {e}", exc_info=getattr(e, "original_exception", None) or True)
    # Still a friendly landing, but no longer silent: the error is recorded
    # and the user is told something went wrong instead of being dropped on
    # the login page as if nothing had happened.
    try:
        flash("Something went wrong on that page. It has been recorded for the admin.", "danger")
    except Exception:
        pass
    return redirect(url_for("index"))


@app.errorhandler(Exception)
def unhandled_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    return server_error(e)


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
        flash(f"Complaint recorded{tgt_note}. Deductions — Picker: {picker_final}pts, Checker: {checker_final}pts, Purchaser: {purchaser_final}pts", "warning")

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
            flash("Complaint marked as resolved.", "success")
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
    """Admin applies the performance-based target suggestion for this employee.

    Recomputed here from the last 30 days rather than trusted from the form,
    and it only ever moves a personal target UP -- a target that rises with
    performance and never quietly drops.
    """
    try:
        emp = db.session.get(Employee, emp_id)
        if not emp:
            flash("Employee not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        stats = build_analytics(get_period_entries(emp.id, "last30"), emp.staff_type, emp_id=emp.id)
        sug = (stats or {}).get("target_suggestion")
        if not sug:
            flash(f"No target change is suggested for {emp.name} right now.", "info")
            return redirect(url_for("staff_detail", emp_id=emp.id))
        current = sug["current"]
        new_target = sug["suggested"]
        if emp.custom_hourly_target and new_target <= emp.custom_hourly_target:
            flash(f"{emp.name}'s target is already {emp.custom_hourly_target:g}/hr.", "info")
            return redirect(url_for("staff_detail", emp_id=emp.id))
        emp.custom_hourly_target = new_target
        db.session.commit()
        log_audit("raise_target", emp.name,
                  f"Hourly target raised from {current} to {new_target}/hr")
        flash(f"{emp.name}'s hourly target raised to {new_target}/hr (was {current}).", "success")
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
            flash(f"{emp.name}'s target reset to role default.", "success")
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
    """Points for ONE completed delivery (capped at 10):
      • completed            = 5
      • delivered ≤ 60 min   = +3
      • delivered < 30 min   = +2 more
    So a fast delivery earns the full 10; a slow-but-done one earns 5."""
    score = 5  # base completion
    dur = trip.duration_minutes
    if dur is not None:
        if dur <= 60:
            score += 3
        if dur < 30:
            score += 2
    elif trip.is_on_time:
        score += 3
    return min(score, 10)


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
        icon = request.form.get("icon", "").strip()[:10]
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
            if len(password) < 8:
                skipped.append(f"{email}: password too short (min 8)")
                continue
            if staff_type not in VALID_STAFF_TYPES:
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


def report_recipient():
    """Where the monthly report is sent.

    Renamed from ADMIN_EMAIL_RECIPIENT, which sat next to ADMIN_EMAIL (the
    admin's login account) and was easy to set by mistake. REPORT_EMAIL_TO says
    what it does. The old name is still honoured so a deployment that has it set
    keeps working through the change.
    """
    return (os.environ.get("REPORT_EMAIL_TO")
            or os.environ.get("ADMIN_EMAIL_RECIPIENT")
            or "")


def mail_config_status():
    """What is missing before mail can send. Empty list means ready.

    An HTTP transport needs only an API key, so the SMTP variables are only
    required when SMTP is the transport actually in use.
    """
    missing = []
    transport = active_email_transport()
    if transport is None:
        missing.append("BREVO_API_KEY or RESEND_API_KEY (or MAIL_SERVER for SMTP)")
    elif transport == "smtp":
        if not os.environ.get("MAIL_USERNAME"):
            missing.append("MAIL_USERNAME")
        if not os.environ.get("MAIL_PASSWORD"):
            missing.append("MAIL_PASSWORD")
    if not report_recipient():
        missing.append("REPORT_EMAIL_TO")
    return missing


def send_monthly_reports():
    """Email the monthly KPI PDFs to the admin.

    Returns (ok, message). It used to return None whether it sent or silently
    gave up, so the caller could not tell the difference and the admin was shown
    a success message for an email that was never sent.
    """
    with app.app_context():
        missing = mail_config_status()
        if missing:
            msg = "Mail is not configured. Missing: " + ", ".join(missing)
            logger.warning(msg + " — monthly reports not sent.")
            return False, msg

        admin_email = report_recipient()
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
            # Most SMTP providers reject messages over ~25MB, and one oversized
            # send would drop every report. Fill up to a safe budget and say
            # plainly which staff did not fit rather than failing the whole send.
            BUDGET = 18 * 1024 * 1024
            used, attached, skipped, failed = 0, 0, [], []
            for emp in employees:
                try:
                    payload = build_pdf_payload(emp)
                    pdf_buf = generate_visual_pdf(emp.name, payload)
                    data = pdf_buf.getvalue()
                    if used + len(data) > BUDGET:
                        skipped.append(emp.name)
                        continue
                    safe_name = emp.name.replace(" ", "_").replace("/", "-")
                    msg.attach(
                        f"KRA_{safe_name}_{now.strftime('%Y-%m')}.pdf",
                        "application/pdf",
                        data
                    )
                    used += len(data)
                    attached += 1
                except Exception as pe:
                    logger.error(f"PDF for {emp.name}: {pe}")
                    failed.append(emp.name)

            if not attached:
                m = "No reports could be generated, so nothing was sent."
                logger.error("send_monthly_reports: " + m)
                return False, m

            if skipped:
                msg.body += ("\n\nNot attached, the message would have been too large: "
                             + ", ".join(skipped))
            if failed:
                msg.body += "\n\nCould not be generated: " + ", ".join(failed)

            transport = active_email_transport()
            if transport in ("brevo", "resend"):
                # The HTTP path sends text only. Attachments would need base64
                # encoding per provider; rather than silently drop the PDFs,
                # the message says where to get them.
                summary = (msg.body + "\n\nThe attached reports are not included "
                           "when sending over the HTTP email API. Download them "
                           "from Reports Archive in the admin dashboard.")
                sender_addr = (os.environ.get("MAIL_USERNAME")
                               or os.environ.get("EMAIL_FROM") or "noreply@pharmaip.com")
                fn = _send_via_brevo if transport == "brevo" else _send_via_resend
                sent_ok, sent_note = fn(admin_email, msg.subject, summary, sender_addr)
                if not sent_ok:
                    return False, sent_note
            else:
                with mail_socket_timeout():
                    mail.send(msg)
            note = f"Sent {attached} report(s) to {admin_email}"
            if skipped:
                note += f"; {len(skipped)} left out for size"
            if failed:
                note += f"; {len(failed)} failed to generate"
            logger.info("send_monthly_reports: " + note)
            return True, note
        except Exception as e:
            logger.error(f"send_monthly_reports: {e}")
            return False, f"Mail server rejected the message: {e}"


@app.route("/admin/send_monthly_report")
@admin_required
def admin_send_monthly_report():
    """Manual trigger for monthly PDF email."""
    try:
        ok, note = send_monthly_reports()
        flash(note, "success" if ok else "danger")
    except Exception as e:
        logger.error(f"admin_send_monthly_report: {e}")
        flash(f"Error sending reports: {e}", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/day")
@admin_required
def admin_day_view():
    """Every staff member's figures for one chosen date, with prev/next.

    There was no way to look back at a particular day across the whole team:
    history lived on each person's profile, one at a time. Defaults to the most
    recent day that has entries, so it never opens on an empty page.
    """
    from sqlalchemy import func
    try:
        chosen = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        chosen = (db.session.query(func.max(KPIEntry.entry_date))
                  .filter(KPIEntry.entry_date <= date.today()).scalar()) or date.today()
    prev_day = (db.session.query(func.max(KPIEntry.entry_date))
                .filter(KPIEntry.entry_date < chosen).scalar())
    next_day = (db.session.query(func.min(KPIEntry.entry_date))
                .filter(KPIEntry.entry_date > chosen, KPIEntry.entry_date <= date.today()).scalar())

    rows = []
    for e, emp in (db.session.query(KPIEntry, Employee)
                   .join(Employee, Employee.id == KPIEntry.emp_id)
                   .filter(KPIEntry.entry_date == chosen)
                   .order_by(Employee.staff_type, Employee.name).all()):
        st = emp.staff_type
        stats = build_analytics([e], st, emp_id=emp.id) or {}
        if st == "checker":
            figs = [("Bills checked", (e.sales_bills_open or 0) + (e.cs_sales_open or 0)),
                    ("of which urgent", e.cs_sales_open or 0),
                    ("Items checked", (e.checked or 0) + (e.errors_found or 0))]
        elif st == "purchaser":
            figs = [("PO bills", e.sales_bills_open or 0), ("PO checked", e.checked or 0),
                    ("Items", e.errors_found or 0)]
        else:
            figs = [("Bills given", e.total_bills_received or 0),
                    ("Bills picked", e.sales_bills_open or 0),
                    ("Items picked", e.picked or 0), ("Missed", e.missed or 0)]
        rows.append({"emp": emp, "figs": figs,
                     "score": stats.get("eff_score"), "grade": stats.get("grade")})

    team = checker_team_day_totals([chosen]).get(chosen) or {}
    mts = (db.session.query(MultitaskEntry, Employee)
           .join(Employee, Employee.id == MultitaskEntry.emp_id)
           .filter(MultitaskEntry.entry_date == chosen)
           .order_by(MultitaskEntry.secondary_type, Employee.name).all())
    return render_template("admin_day.html", chosen=chosen, prev_day=prev_day,
                           next_day=next_day, rows=rows, team=team, mts=mts,
                           role_label=multitask_role_label)


@app.route("/admin/multitask_report")
@admin_required
def admin_multitask_report():
    """Who covered which extra roles, one chart per person.

    The admin panel's multitask panel is a flat list of every extra-role report
    in date order, which answers none of the questions worth asking. This page
    is the same data turned round: per person, then per role.
    """
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = days if days in (7, 30, 90, 365) else 30
    report = multitask_report(days)
    return render_template("admin_multitask_report.html",
                           report=report, days=days,
                           role_order=MULTITASK_ROLE_ORDER,
                           role_label=multitask_role_label)


@app.route("/admin/diagnostics")
@admin_required
def admin_diagnostics():
    """What is and is not configured on this deployment.

    Most of this app degrades quietly when an environment variable is missing:
    mail silently does not send, the delivery geofence silently switches off,
    SMS silently does nothing. That is good for uptime and terrible for working
    out why a feature "does not work". This page states it plainly.
    """
    def status(var, needed_for, secret=True):
        raw = os.environ.get(var, "")
        return {"var": var, "set": bool(raw), "needed_for": needed_for,
                "value": ("set" if secret else raw) if raw else ""}

    mail_vars = [
        status("MAIL_SERVER", "Monthly report email", secret=False),
        status("MAIL_PORT", "Monthly report email", secret=False),
        status("MAIL_USERNAME", "Monthly report email", secret=False),
        status("MAIL_PASSWORD", "Monthly report email"),
        {"var": "REPORT_EMAIL_TO", "set": bool(report_recipient()),
         "needed_for": "Who the monthly report is sent to", "value": report_recipient()},
    ]
    delivery_vars = [
        status("STORE_LAT", "Geofence: riders must be at the store to start a trip", secret=False),
        status("STORE_LNG", "Geofence: riders must be at the store to start a trip", secret=False),
        status("TOMTOM_API_KEY", "Route distances on the delivery map"),
    ]
    sms_vars = [
        status("TWILIO_ACCOUNT_SID", "Low-score and stale-validation SMS alerts"),
        status("TWILIO_AUTH_TOKEN", "Low-score and stale-validation SMS alerts"),
        status("TWILIO_FROM_NUMBER", "Low-score and stale-validation SMS alerts", secret=False),
        status("ADMIN_PHONE", "Who the SMS alerts go to", secret=False),
    ]
    core_vars = [
        status("DATABASE_URL", "Supabase database (SQLite is used when unset)"),
        status("SECRET_KEY", "Login sessions survive a restart"),
        status("ADMIN_PASSWORD", "Admin login (defaults to admin123 when unset)"),
        status("PICKER_TARGET_HR", "Picker volume target", secret=False),
        status("CHECKER_TARGET_HR", "Checker volume target", secret=False),
    ]

    warnings = []
    if not os.environ.get("DATABASE_URL"):
        warnings.append("No DATABASE_URL: this instance is on local SQLite, so data "
                        "does not persist across restarts and is not your Supabase data.")
    if os.environ.get("ADMIN_PASSWORD", "admin123") == "admin123":
        warnings.append("The admin password is still the default 'admin123'. "
                        "Set ADMIN_PASSWORD.")
    if not os.environ.get("SECRET_KEY"):
        warnings.append("No SECRET_KEY: a random one is generated on each boot, so "
                        "every user is logged out whenever the service restarts.")
    try:
        if float(os.environ.get("STORE_LAT", "0") or 0) == 0.0:
            warnings.append("STORE_LAT is unset, so the delivery geofence is switched "
                            "off entirely: a rider can start a trip from anywhere.")
    except ValueError:
        warnings.append("STORE_LAT is not a number, so the geofence is off.")
    if mail_config_status():
        warnings.append("Mail is not configured, so the monthly report email cannot "
                        "send. Missing: " + ", ".join(mail_config_status()))

    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok, db_note = True, ("PostgreSQL / Supabase" if os.environ.get("DATABASE_URL")
                                else "SQLite (local file)")
    except Exception as e:
        db_ok, db_note = False, str(e)[:160]

    errors = recent_errors(7)
    if errors:
        warnings.append("%d different error(s) happened on the live app in the last 7 days "
                        "(%d times in total) -- listed under Recent Errors below."
                        % (len(errors), sum(e.count or 1 for e in errors)))
    return render_template("admin_diagnostics.html", errors=errors,
                           groups=[("Email", mail_vars), ("Delivery", delivery_vars),
                                   ("SMS alerts", sms_vars), ("Core", core_vars)],
                           warnings=warnings, db_ok=db_ok, db_note=db_note,
                           mail_missing=mail_config_status(),
                           transport=active_email_transport() or 'none',
                           location=location_data_summary(),
                           is_production=bool(IS_PRODUCTION))



MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "15"))

# ─── EMAIL TRANSPORT ─────────────────────────────────────────────────────────
# Render blocks outbound SMTP on free web services (ports 25, 465 and 587,
# since September 2025), so no SMTP credentials can work there however correct
# they are: the connection simply never completes. Render's own guidance is to
# use a transactional email API over HTTPS, which is not blocked.
#
# So mail goes out over HTTP when an API key is present, and falls back to SMTP
# otherwise, which keeps working on a paid instance or any other host.
# Set ONE of these:
#   BREVO_API_KEY   - brevo.com, 300 emails/day free
#   RESEND_API_KEY  - resend.com, 3000 emails/month free
EMAIL_PROVIDER = (os.environ.get("EMAIL_PROVIDER", "auto") or "auto").lower()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")


def active_email_transport():
    """Which transport will actually be used: 'brevo', 'resend', 'smtp' or None."""
    if EMAIL_PROVIDER == "brevo" and BREVO_API_KEY:
        return "brevo"
    if EMAIL_PROVIDER == "resend" and RESEND_API_KEY:
        return "resend"
    if EMAIL_PROVIDER == "smtp":
        return "smtp" if os.environ.get("MAIL_SERVER") else None
    if BREVO_API_KEY:
        return "brevo"
    if RESEND_API_KEY:
        return "resend"
    if os.environ.get("MAIL_SERVER"):
        return "smtp"
    return None


def _send_via_brevo(to, subject, body, sender):
    r = _requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json",
                 "accept": "application/json"},
        json={"sender": {"email": sender, "name": "Pharma IP System"},
              "to": [{"email": to}], "subject": subject, "textContent": body},
        timeout=MAIL_TIMEOUT)
    if r.status_code in (200, 201, 202):
        return True, f"Sent to {to} via Brevo."
    return False, f"Brevo refused it ({r.status_code}): {r.text[:180]}"


def _send_via_resend(to, subject, body, sender):
    r = _requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": "Bearer " + RESEND_API_KEY,
                 "Content-Type": "application/json"},
        json={"from": sender, "to": [to], "subject": subject, "text": body},
        timeout=MAIL_TIMEOUT)
    if r.status_code in (200, 201, 202):
        return True, f"Sent to {to} via Resend."
    return False, f"Resend refused it ({r.status_code}): {r.text[:180]}"




from contextlib import contextmanager as _contextmanager


@_contextmanager
def mail_socket_timeout():
    """Bound SMTP socket waits, and only those.

    Flask-Mail does not pass a timeout to smtplib, so a wrong host or a blocked
    port hangs the worker instead of erroring. socket.setdefaulttimeout is the
    only lever that reaches inside it, but it is process-wide: left on, it would
    also cap database queries and kill any slow one. So it is raised only around
    a send and restored immediately afterwards, even if the send raises.
    """
    import socket
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(MAIL_TIMEOUT)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)




def send_test_email(to, subject=None, body=None):
    """Send one small message over the active transport, failing fast and
    saying exactly what went wrong.

    Flask-Mail opens the SMTP socket with no timeout, so a wrong host or a
    blocked port does not error -- it hangs. With one eventlet worker that
    blocks the whole service until gunicorn kills the request at 120 seconds,
    which is why the button appeared to do nothing for a long time and then
    gave up. smtplib is used directly here so the timeout is ours, and so each
    failure can be reported as something actionable rather than as a stall.

    Returns (ok, message).
    """
    import smtplib
    import socket
    from email.message import EmailMessage

    subject = subject or "Pharma IP System - test email"
    body = body or "Mail is configured correctly. Sent from the diagnostics page."
    transport = active_email_transport()
    sender_addr = (os.environ.get("MAIL_USERNAME")
                   or os.environ.get("EMAIL_FROM") or "noreply@pharmaip.com")

    if transport == "brevo":
        try:
            return _send_via_brevo(to, subject, body, sender_addr)
        except Exception as e:
            return False, f"Brevo request failed: {str(e)[:160]}"
    if transport == "resend":
        try:
            return _send_via_resend(to, subject, body, sender_addr)
        except Exception as e:
            return False, f"Resend request failed: {str(e)[:160]}"
    if transport is None:
        return False, ("No email transport configured. Set BREVO_API_KEY or "
                       "RESEND_API_KEY, or MAIL_SERVER for SMTP.")

    host = os.environ.get("MAIL_SERVER", "")
    port = int(os.environ.get("MAIL_PORT", 587) or 587)
    user = os.environ.get("MAIL_USERNAME", "")
    pwd = os.environ.get("MAIL_PASSWORD", "")
    sender = app.config.get("MAIL_DEFAULT_SENDER") or user
    use_tls = os.environ.get("MAIL_USE_TLS", "true").lower() != "false"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)

    try:
        # Port 465 is implicit SSL; 587 and 25 are plain with STARTTLS.
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=MAIL_TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=MAIL_TIMEOUT)
        with server:
            server.ehlo()
            if port != 465 and use_tls:
                server.starttls()
                server.ehlo()
            if user and pwd:
                server.login(user, pwd)
            server.send_message(msg)
        logger.info("Test email sent to %s via %s:%s", to, host, port)
        return True, f"Test email sent to {to}."
    except socket.timeout:
        return False, (f"Timed out after {MAIL_TIMEOUT}s connecting to {host}:{port}. "
                       "Render blocks outbound SMTP on free web services (ports 25, "
                       "465 and 587), so no SMTP settings can work there. Set "
                       "BREVO_API_KEY or RESEND_API_KEY to send over HTTPS instead, "
                       "or upgrade to a paid instance.")
    except smtplib.SMTPAuthenticationError as e:
        return False, ("The mail server rejected the username or password. For Gmail "
                       "you must use a 16-character App Password, not the account "
                       f"password. ({str(e)[:120]})")
    except smtplib.SMTPSenderRefused:
        return False, (f"The server refused '{sender}' as the sender. It usually has to "
                       "match MAIL_USERNAME.")
    except smtplib.SMTPRecipientsRefused:
        return False, f"The server refused the recipient address '{to}'. Check REPORT_EMAIL_TO."
    except (socket.gaierror, OSError) as e:
        return False, (f"Could not reach {host}:{port} - {str(e)[:120]}. Check MAIL_SERVER "
                       "and MAIL_PORT, and that the host allows outbound SMTP.")
    except Exception as e:
        logger.error(f"send_test_email: {e}")
        return False, f"{type(e).__name__}: {str(e)[:160]}"


@app.route("/admin/test_mail", methods=["POST"])
@admin_required
def admin_test_mail():
    """Send one small test email so mail can be proven without the full report."""
    missing = mail_config_status()
    if missing:
        flash("Mail is not configured. Missing: " + ", ".join(missing), "danger")
        return redirect(url_for("admin_diagnostics"))
    to = report_recipient()
    ok, detail = send_test_email(to)
    flash(detail, "success" if ok else "danger")
    return redirect(url_for("admin_diagnostics"))


def auto_raise_targets():
    """Raise personal targets automatically when performance has risen.

    Runs on the 1st of each month. For every picker and checker who already
    has a personal target, if their 75th-percentile day over the last 30 days
    is above it, the target moves up to meet it. It only ever moves up, and
    only for people on a personal target: replacing the role default with a
    first personal target lowers the bar, so that stays a one-click decision
    for the admin on the staff profile.
    """
    raised = []
    with app.app_context():
        try:
            staff = Employee.query.filter(Employee.is_admin.is_(False),
                                          Employee.custom_hourly_target.isnot(None),
                                          Employee.staff_type.in_(("picker", "checker"))).all()
            for emp in staff:
                st = build_analytics(get_period_entries(emp.id, "last30"), emp.staff_type, emp_id=emp.id)
                sug = (st or {}).get("target_suggestion")
                if sug and sug.get("kind") == "raise" and sug["suggested"] > (emp.custom_hourly_target or 0):
                    old = emp.custom_hourly_target
                    emp.custom_hourly_target = sug["suggested"]
                    db.session.add(AuditLog(admin_id=None, action="auto_raise_target", target=emp.name,
                                            details="Pace rose: target %g -> %g/hr" % (old, sug["suggested"])))
                    raised.append((emp.name, old, sug["suggested"]))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("auto_raise_targets: %s", exc)
    if raised:
        logger.info("auto_raise_targets: %s", raised)
    return raised


def purge_old_location_data():
    """Delete GPS breadcrumbs older than the retention window.

    Keeping a rider's minute-by-minute movements indefinitely is not needed to
    review deliveries, and it is the hardest thing in this system to justify
    holding. Trips, stops and delivery records are kept: those are the business
    record. Only the fine-grained location trail expires.

    Runs daily and on boot. Returns (deleted, cutoff).
    """
    with app.app_context():
        try:
            cutoff = datetime.utcnow() - timedelta(days=GPS_RETENTION_DAYS)
            n = (BreadcrumbPoint.query
                 .filter(BreadcrumbPoint.recorded_at < cutoff)
                 .delete(synchronize_session=False))
            # The error log only needs to cover the recent past.
            ErrorLog.query.filter(ErrorLog.day < date.today() - timedelta(days=30)) \
                .delete(synchronize_session=False)
            db.session.commit()
            if n:
                logger.info("Privacy: purged %d GPS point(s) older than %d days"
                            % (n, GPS_RETENTION_DAYS))
            return n, cutoff
        except Exception as e:
            db.session.rollback()
            logger.error(f"purge_old_location_data: {e}")
            return 0, None


def location_data_summary():
    """How much location data is held, for the diagnostics page."""
    try:
        total = BreadcrumbPoint.query.count()
        oldest = (BreadcrumbPoint.query
                  .order_by(BreadcrumbPoint.recorded_at.asc()).first())
        return {
            "points": total,
            "oldest": oldest.recorded_at if oldest else None,
            "retention_days": GPS_RETENTION_DAYS,
            "min_seconds": GPS_MIN_SECONDS,
            "min_metres": GPS_MIN_METRES,
        }
    except Exception:
        return {"points": 0, "oldest": None, "retention_days": GPS_RETENTION_DAYS,
                "min_seconds": GPS_MIN_SECONDS, "min_metres": GPS_MIN_METRES}


@app.route("/my-data")
@login_required
def my_data():
    """What this system holds about the signed-in person, and who can see it.

    Staff can be measured, scored and location-tracked by this app without ever
    being shown what is recorded. This page states it plainly and lets them
    take a copy.
    """
    emp_id = session.get("user_id")
    emp = db.session.get(Employee, emp_id)
    if not emp:
        return redirect(url_for("logout"))

    def count(model, **kw):
        try:
            return model.query.filter_by(**kw).count()
        except Exception:
            return 0

    try:
        trip_ids = [t.id for t in DeliveryTrip.query.filter_by(emp_id=emp_id).all()]
        gps_points = (BreadcrumbPoint.query
                      .filter(BreadcrumbPoint.trip_id.in_(trip_ids)).count()
                      if trip_ids else 0)
    except Exception:
        gps_points = 0

    held = [
        {"what": "Name, email and role", "n": 1,
         "why": "Identifies your account and decides which screens you see.",
         "who": "You and the admin."},
        {"what": "Phone number", "n": 1 if emp.phone else 0,
         "why": "Used only to send a one-time code if you reset your password.",
         "who": "You and the admin."},
        {"what": "Daily KPI entries", "n": count(KPIEntry, emp_id=emp_id),
         "why": "The numbers you submit each day, used to calculate your score.",
         "who": "You and the admin."},
        {"what": "Extra-role (multitask) reports", "n": count(MultitaskEntry, emp_id=emp_id),
         "why": "Work you logged under a second role.",
         "who": "You and the admin."},
        {"what": "Daily work notes", "n": count(DeliveryBillerNote, emp_id=emp_id),
         "why": "Your end-of-day note.", "who": "You and the admin."},
        {"what": "Checklist completions", "n": count(ChecklistCompletion, emp_id=emp_id),
         "why": "Which daily tasks you ticked off, and when.",
         "who": "You and the admin."},
        {"what": "Delivery trips", "n": count(DeliveryTrip, emp_id=emp_id),
         "why": "Start and finish times of rounds you drove.",
         "who": "You and the admin."},
        {"what": "GPS location points", "n": gps_points,
         "why": ("Recorded only while a delivery trip is running, to show the route "
                 "taken. Never recorded before you start a trip or after you finish "
                 "one. Automatically deleted after %d days." % GPS_RETENTION_DAYS),
         "who": "You and the admin."},
        {"what": "Admin notes about you", "n": count(AdminStaffNote, emp_id=emp_id),
         "why": "Notes your manager recorded.", "who": "The admin."},
    ]
    return render_template("my_data.html", emp=emp, held=held,
                           gps_retention_days=GPS_RETENTION_DAYS,
                           gps_min_seconds=GPS_MIN_SECONDS,
                           gps_min_metres=GPS_MIN_METRES)


@app.route("/my-data/download")
@login_required
def my_data_download():
    """Give the signed-in person a copy of their own records as JSON."""
    emp_id = session.get("user_id")
    emp = db.session.get(Employee, emp_id)
    if not emp:
        return redirect(url_for("logout"))
    try:
        def rows(model, **kw):
            out = []
            for r in model.query.filter_by(**kw).all():
                d = {}
                for c in r.__table__.columns:
                    v = getattr(r, c.name, None)
                    d[c.name] = v.isoformat() if hasattr(v, "isoformat") else v
                out.append(d)
            return out

        payload = {
            "generated_at": datetime.utcnow().isoformat(),
            "about": {"name": emp.name, "email": emp.email, "role": emp.staff_type,
                      "secondary_role": emp.secondary_staff_type, "phone": emp.phone},
            "kpi_entries": rows(KPIEntry, emp_id=emp_id),
            "multitask_entries": rows(MultitaskEntry, emp_id=emp_id),
            "daily_notes": rows(DeliveryBillerNote, emp_id=emp_id),
            "checklist_completions": rows(ChecklistCompletion, emp_id=emp_id),
            "delivery_trips": rows(DeliveryTrip, emp_id=emp_id),
            "note": ("Your password is stored only as a one-way hash and is not "
                     "included here, because it cannot be read back by anyone, "
                     "including the admin."),
        }
        return Response(
            json.dumps(payload, indent=2, default=str),
            mimetype="application/json",
            headers={"Content-Disposition":
                     'attachment; filename="my-data-%s.json"'
                     % datetime.utcnow().strftime("%Y-%m-%d")})
    except Exception as e:
        logger.error(f"my_data_download: {e}")
        flash("Could not build your data file.", "danger")
        return redirect(url_for("my_data"))


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
        flash(f"Note added for {emp.name}.", "success")
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
        # Limit OTP sends per phone (anti SMS-flood): 5 per 10 minutes
        _sk = f"otpsend:{phone}"
        if _throttle_check(_sk):
            flash("Too many OTP requests. Please wait a few minutes and try again.", "warning")
            return render_template("forgot_password.html")
        _throttle_fail(_sk)
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
        if len(new_pw) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("reset_password.html", phone=phone)
        emp = Employee.query.filter_by(phone=phone).first()
        if not emp:
            flash("Invalid or expired reset link.", "danger")
            return redirect(url_for("forgot_password"))
        # OTP brute-force lockout: 5 wrong codes per phone → 10 min lock
        _ok = f"otp:{phone}"
        _wait = _throttle_check(_ok)
        if _wait:
            flash(f"Too many wrong OTP attempts. Try again in {max(_wait // 60, 1)} minute(s).", "danger")
            return render_template("reset_password.html", phone=phone)
        otp_obj = PasswordResetOTP.query.filter_by(
            emp_id=emp.id, otp_code=otp_input, is_used=False
        ).filter(PasswordResetOTP.expires_at >= datetime.utcnow()).first()
        if not otp_obj:
            _throttle_fail(_ok)
            flash("OTP is invalid or has expired. Please request a new one.", "danger")
            return render_template("reset_password.html", phone=phone)
        _throttle_clear(_ok)
        emp.set_password(new_pw)
        otp_obj.is_used = True
        db.session.commit()
        log_audit("password_reset_otp", emp.name, "Password reset via phone OTP")
        flash("Password reset successfully. Please log in.", "success")
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
            logger.info(f"Monthly report {month_str} generated: {pdf_count} PDFs")
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


STORE_LAT = float(os.environ.get("STORE_LAT", "0.0"))
STORE_LNG = float(os.environ.get("STORE_LNG", "0.0"))
# Fallback city centre (Cuttack) when no STORE_LAT/LNG configured — keeps the
# delivery maps rendering even when geocoding fails for a store.
FALLBACK_LAT = STORE_LAT if STORE_LAT else 20.4625
FALLBACK_LNG = STORE_LNG if STORE_LNG else 85.8830
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


def _haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in kilometres between two points (0 if any coord missing)."""
    try:
        if None in (lat1, lng1, lat2, lng2):
            return 0.0
        return round(_haversine_m(float(lat1), float(lng1), float(lat2), float(lng2)) / 1000.0, 2)
    except (TypeError, ValueError):
        return 0.0


def compute_stop_distances(stops, base_lat=None, base_lng=None):
    """Fill each stop's dist_from_prev_km (from the store base, then chained). Returns total km."""
    prev_lat = base_lat if base_lat else (STORE_LAT or None)
    prev_lng = base_lng if base_lng else (STORE_LNG or None)
    total = 0.0
    for s in stops:
        if s.lat and s.lng and prev_lat and prev_lng:
            d = _haversine_km(prev_lat, prev_lng, s.lat, s.lng)
            s.dist_from_prev_km = d
            total += d
            prev_lat, prev_lng = s.lat, s.lng
        else:
            s.dist_from_prev_km = s.dist_from_prev_km or 0.0
    return round(total, 2)


def _assigner_day_summary(assigner_id, day, emp_map=None):
    """Return a dict summarising everything dispatched by `assigner_id` on `day`."""
    emp_map = emp_map or {}
    assignments = (DeliveryAssignment.query
                   .filter_by(purchaser_id=assigner_id)
                   .filter(db.func.date(DeliveryAssignment.assigned_at) == day)
                   .order_by(DeliveryAssignment.assigned_at.asc())
                   .all())
    if not assignments:
        return {
            "total_dispatches": 0, "total_packets": 0,
            "delivered": 0, "pending": 0, "in_transit": 0, "failed": 0,
            "per_rider": [], "assignments": [],
        }
    aid_list = [a.id for a in assignments]
    stops = DeliveryStop.query.filter(DeliveryStop.assignment_id.in_(aid_list)).order_by(DeliveryStop.id).all()
    trips = DeliveryTrip.query.filter(DeliveryTrip.assignment_id.in_(aid_list)).all()
    trip_map = {t.assignment_id: t for t in trips}
    stops_map = {}
    for st in stops:
        stops_map.setdefault(st.assignment_id, []).append(st)

    total_packets = 0
    per_rider = {}
    for a in assignments:
        total_packets += a.no_of_tasks or 0
        rid = a.delivery_emp_id
        if rid not in per_rider:
            emp = emp_map.get(rid)
            per_rider[rid] = {
                "name": emp.name if emp else f"#{rid}",
                "dispatches": 0, "packets": 0,
                "delivered": 0, "pending": 0, "in_transit": 0, "failed": 0,
                "total_km": 0.0,
            }
        per_rider[rid]["dispatches"] += 1
        per_rider[rid]["packets"] += a.no_of_tasks or 0
        per_rider[rid][a.status if a.status in ("delivered", "in_transit", "failed") else "pending"] += 1
        for st in stops_map.get(a.id, []):
            per_rider[rid]["total_km"] = round(per_rider[rid]["total_km"] + (st.dist_from_prev_km or 0), 2)

    statuses = [a.status for a in assignments]
    return {
        "total_dispatches": len(assignments),
        "total_packets": total_packets,
        "delivered": statuses.count("delivered"),
        "pending": statuses.count("pending"),
        "in_transit": statuses.count("in_transit"),
        "failed": statuses.count("failed"),
        "per_rider": sorted(per_rider.values(), key=lambda r: r["name"]),
        "assignments": assignments,
        "stops_map": stops_map,
        "trip_map": trip_map,
    }


def geocode_address(address):
    """Convert an Odisha address to (lat, lng) using Nominatim, biased to Odisha state.
    Tries hard: appends ', Odisha, India' if missing, then falls back to a wider search.
    """
    if not address or not address.strip():
        return None, None
    q = address.strip()
    if "odisha" not in q.lower() and "orissa" not in q.lower():
        q_biased = f"{q}, Odisha, India"
    else:
        q_biased = q
    # Odisha bounding box: roughly lon 81.3..87.5, lat 17.7..22.6
    viewbox = "81.3,22.6,87.5,17.7"  # left,top,right,bottom
    headers = {"User-Agent": "IndianPharmaKPI/1.0 (delivery-geocoder)"}
    try:
        # 1st attempt: strict to Odisha viewbox
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q_biased, "format": "json", "limit": 1,
                    "countrycodes": "in", "viewbox": viewbox, "bounded": 1},
            headers=headers, timeout=6,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
        # 2nd: relax bounded, keep viewbox as preference
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q_biased, "format": "json", "limit": 1,
                    "countrycodes": "in", "viewbox": viewbox},
            headers=headers, timeout=6,
        )
        results = resp.json()
        if results:
            lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
            # sanity check: stay inside Odisha bbox
            if 17.7 <= lat <= 22.6 and 81.3 <= lng <= 87.5:
                return lat, lng
    except Exception as e:
        logger.warning(f"geocode_address failed for '{address}': {e}")
    return None, None


@app.route("/delivery/assign", methods=["POST"])
@login_required
def delivery_assign():
    """Assigner dispatches a multi-store route to a delivery employee.
    Each store row = name + package count; total tasks = sum of packages.
    Creates one DeliveryAssignment + a DeliveryStop per store, then notifies the rider."""
    emp_id = session.get("user_id")
    staff_type = session.get("staff_type", "")
    if staff_type not in ("biller",) and not session.get("is_admin"):
        flash("Only the Assigner can assign deliveries.", "danger")
        return redirect(url_for("dashboard"))
    try:
        delivery_emp_id = int(request.form.get("delivery_emp_id", 0) or 0)
        packet_type     = (request.form.get("packet_type", "") or "").strip()[:50]
        notes           = (request.form.get("notes", "") or "").strip()[:500]

        # Multi-store line items (parallel arrays from the form)
        store_names    = request.form.getlist("store_name[]")
        store_areas    = request.form.getlist("store_area[]")   # new: delivery area separate from store name
        store_packages = request.form.getlist("store_packages[]")
        store_lats     = request.form.getlist("store_lat[]")
        store_lngs     = request.form.getlist("store_lng[]")
        store_ptypes   = request.form.getlist("store_packet_type[]")

        # Build a clean list, dropping blank rows (store name is required; area is optional)
        stores = []
        for i, raw_name in enumerate(store_names):
            nm = (raw_name or "").strip()
            if not nm:
                continue
            ar = (store_areas[i].strip()[:150] if i < len(store_areas) and store_areas[i] else "")
            try:
                pkg = max(1, int(store_packages[i])) if i < len(store_packages) and store_packages[i] else 1
            except (ValueError, TypeError):
                pkg = 1
            slat = slng = None
            try:
                if i < len(store_lats) and store_lats[i] and i < len(store_lngs) and store_lngs[i]:
                    _la, _ln = float(store_lats[i]), float(store_lngs[i])
                    if 17.7 <= _la <= 22.6 and 81.3 <= _ln <= 87.5:
                        slat, slng = _la, _ln
            except (ValueError, TypeError):
                pass
            spt = (store_ptypes[i].strip()[:50] if i < len(store_ptypes) and store_ptypes[i] else "") or packet_type
            # Geocode against "Store Name, Area, Odisha" for better accuracy
            geo_hint = f"{nm}, {ar}" if ar else nm
            stores.append({"name": nm[:150], "area": ar or None, "packages": pkg,
                           "lat": slat, "lng": slng, "packet_type": spt or None,
                           "geo_hint": geo_hint})

        if not delivery_emp_id or not stores:
            flash("Pick a delivery employee and add at least one store.", "warning")
            return redirect(url_for("dashboard"))

        delivery_emp = db.session.get(Employee, delivery_emp_id)
        _p = (getattr(delivery_emp, "staff_type", "") or "").lower() if delivery_emp else ""
        _s = (getattr(delivery_emp, "secondary_staff_type", "") or "").lower() if delivery_emp else ""
        if not delivery_emp or (_p != "delivery" and _s != "delivery"):
            flash("Selected employee is not a delivery staff member.", "warning")
            return redirect(url_for("dashboard"))

        total_tasks = sum(s["packages"] for s in stores) or len(stores)
        route_str = ", ".join(
            (f"{s['name']} ({s['area']})" if s.get("area") else s["name"]) for s in stores
        )[:300]
        pkg_desc = f"{total_tasks} packet(s) across {len(stores)} store(s)" + (f" — {packet_type}" if packet_type else "")

        # Geocode any store missing a manual pin — use "Store Name, Area" for better accuracy.
        # If geocoding fails (service down / store unknown) fall back to the store-base
        # coordinates so the rider's map and the admin route map ALWAYS render.
        for s in stores:
            if s["lat"] is None:
                s["lat"], s["lng"] = geocode_address(s.get("geo_hint") or s["name"])
            if s["lat"] is None:
                s["lat"], s["lng"] = FALLBACK_LAT, FALLBACK_LNG
                logger.warning(f"geocode fallback used for store '{s['name']}' — set a manual pin for accuracy")

        first = stores[0]
        now = datetime.utcnow()
        assignment = DeliveryAssignment(
            purchaser_id=emp_id,
            delivery_emp_id=delivery_emp_id,
            destination_addr=route_str,
            destination_lat=first["lat"],
            destination_lng=first["lng"],
            package_desc=pkg_desc,
            bills_count=total_tasks,
            recipient_name=None,
            company_name=None,
            notes=notes,
            status="pending",
            route=route_str,
            no_of_tasks=total_tasks,
            packet_type=packet_type or None,
            dispatch_time=now,
            no_of_task_return=0,
        )
        db.session.add(assignment)
        db.session.flush()   # get assignment.id

        stop_objs = []
        for s in stores:
            so = DeliveryStop(
                assignment_id=assignment.id,
                emp_id=delivery_emp_id,
                place_name=s["name"],
                area=s.get("area"),
                packages=s["packages"],
                reached_at=None,
                lat=s["lat"],
                lng=s["lng"],
                packet_type=s.get("packet_type"),
            )
            db.session.add(so)
            stop_objs.append(so)
        # Compute straight-line km between stops (store base → stop1 → stop2 …)
        total_km = compute_stop_distances(stop_objs, first["lat"], first["lng"])
        db.session.commit()

        # Notify the rider — in-app bell + SMS (if admin saved a phone number)
        notify_employee(
            delivery_emp_id,
            "New delivery assigned",
            f"{total_tasks} packet(s) — {route_str[:120]}",
            link="/dashboard",
        )
        log_audit("delivery_assign", delivery_emp.name, f"{total_tasks} tasks / {len(stores)} stores")
        flash(f"Assigned {total_tasks} packet(s) across {len(stores)} store(s) to {delivery_emp.name}. Notification sent.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_assign: {e}")
        flash("Error creating delivery assignment.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/delivery/dispatch/<int:assignment_id>/edit", methods=["POST"])
@login_required
def delivery_dispatch_edit(assignment_id):
    """Assigner edits a dispatch's stores (only while pending). Replaces the store rows."""
    emp_id = session.get("user_id")
    staff_type = session.get("staff_type", "")
    if staff_type not in ("biller",) and not session.get("is_admin"):
        flash("Only the Assigner can edit dispatches.", "danger")
        return redirect(url_for("dashboard"))
    try:
        a = db.session.get(DeliveryAssignment, assignment_id)
        if not a:
            flash("Dispatch not found.", "warning")
            return redirect(url_for("dashboard"))
        if a.status != "pending":
            flash("Can't edit — the rider has already started this trip.", "warning")
            return redirect(url_for("dashboard"))

        store_names    = request.form.getlist("store_name[]")
        store_areas    = request.form.getlist("store_area[]")
        store_packages = request.form.getlist("store_packages[]")
        store_ptypes   = request.form.getlist("store_packet_type[]")
        pt = (request.form.get("packet_type", "") or "").strip()[:50]
        stores = []
        for i, raw in enumerate(store_names):
            nm = (raw or "").strip()
            if not nm:
                continue
            ar = (store_areas[i].strip()[:150] if i < len(store_areas) and store_areas[i] else "")
            try:
                pkg = max(1, int(store_packages[i])) if i < len(store_packages) and store_packages[i] else 1
            except (ValueError, TypeError):
                pkg = 1
            spt = (store_ptypes[i].strip()[:50] if i < len(store_ptypes) and store_ptypes[i] else "") or pt
            stores.append({"name": nm[:150], "area": ar or None, "packages": pkg,
                           "packet_type": spt or None,
                           "geo_hint": f"{nm}, {ar}" if ar else nm})
        if not stores:
            flash("A dispatch needs at least one store.", "warning")
            return redirect(url_for("dashboard"))

        DeliveryStop.query.filter_by(assignment_id=a.id).delete()
        total = sum(s["packages"] for s in stores) or len(stores)
        a.route = ", ".join(
            (f"{s['name']} ({s['area']})" if s.get("area") else s["name"]) for s in stores
        )[:300]
        a.destination_addr = a.route
        a.no_of_tasks = total
        a.bills_count = total
        if pt:
            a.packet_type = pt
        new_stops = []
        first_lat = first_lng = None
        for s in stores:
            slat, slng = geocode_address(s.get("geo_hint") or s["name"])
            if slat is None:
                slat, slng = FALLBACK_LAT, FALLBACK_LNG
            if first_lat is None:
                first_lat, first_lng = slat, slng
                a.destination_lat, a.destination_lng = slat, slng
            so = DeliveryStop(assignment_id=a.id, emp_id=a.delivery_emp_id,
                              place_name=s["name"], area=s.get("area"),
                              packages=s["packages"],
                              reached_at=None, lat=slat, lng=slng,
                              packet_type=s.get("packet_type"))
            db.session.add(so)
            new_stops.append(so)
        compute_stop_distances(new_stops, first_lat, first_lng)
        db.session.commit()
        notify_employee(a.delivery_emp_id, "Delivery updated",
                        f"Your dispatch was updated — {total} packet(s).", link="/dashboard")
        flash(f"Dispatch updated — {total} packet(s) across {len(stores)} store(s).", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_dispatch_edit: {e}")
        flash("Error updating dispatch.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/delivery/dispatch/<int:assignment_id>/delete", methods=["POST"])
@login_required
def delivery_dispatch_delete(assignment_id):
    """Assigner deletes a dispatch (only while pending)."""
    emp_id = session.get("user_id")
    staff_type = session.get("staff_type", "")
    if staff_type not in ("biller",) and not session.get("is_admin"):
        flash("Only the Assigner can delete dispatches.", "danger")
        return redirect(url_for("dashboard"))
    try:
        a = db.session.get(DeliveryAssignment, assignment_id)
        if not a:
            flash("Dispatch not found.", "warning")
            return redirect(url_for("dashboard"))
        if a.status != "pending":
            flash("Can't delete — the rider has already started this trip.", "warning")
            return redirect(url_for("dashboard"))
        rider = a.delivery_emp_id
        DeliveryStop.query.filter_by(assignment_id=a.id).delete()
        db.session.delete(a)
        db.session.commit()
        notify_employee(rider, "Delivery cancelled", "A pending dispatch was removed.", link="/dashboard")
        flash("Dispatch deleted.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_dispatch_delete: {e}")
        flash("Error deleting dispatch.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/delivery/log_stop/<int:stop_id>", methods=["POST"])
@login_required
def delivery_log_stop(stop_id):
    """Delivery rider marks a store/stop as delivered — records timestamp, who received it,
    and auto-completes the assignment when every stop is done."""
    emp_id = session.get("user_id")
    try:
        stop = db.session.get(DeliveryStop, stop_id)
        if not stop or stop.emp_id != emp_id:
            return jsonify({"ok": False, "error": "Stop not found"}), 404
        if stop.reached_at:
            return jsonify({"ok": False, "error": "Already marked"}), 400

        data = request.get_json(force=True, silent=True) or {}
        now = datetime.utcnow()

        # Time since previous reached stop (or trip departure)
        trip = DeliveryTrip.query.filter_by(assignment_id=stop.assignment_id).first()
        prev = (DeliveryStop.query
                .filter(DeliveryStop.assignment_id == stop.assignment_id,
                        DeliveryStop.reached_at.isnot(None))
                .order_by(DeliveryStop.reached_at.desc()).first())
        base = prev.reached_at if prev else (trip.departure_time if trip else now)
        stop.reached_at = now
        stop.minutes_from_prev = round((now - base).total_seconds() / 60.0, 1) if base else None

        # Store who received the package (saved in note field)
        received_by = (data.get("received_by") or "").strip()[:150]
        company     = (data.get("company") or "").strip()[:150]
        if received_by or company:
            parts = [p for p in [received_by, company] if p]
            stop.note = "RCVD: " + " / ".join(parts)

        # GPS
        try:
            la, ln = float(data.get("lat", 0)), float(data.get("lng", 0))
            if la and ln:
                stop.lat, stop.lng = la, ln
        except (ValueError, TypeError):
            pass

        db.session.flush()  # assign stop.reached_at before counting

        # Check if ALL stops for this assignment are now done
        all_stops = DeliveryStop.query.filter_by(assignment_id=stop.assignment_id).all()
        done  = sum(1 for s in all_stops if s.reached_at)
        total = len(all_stops)
        all_done = (done == total)

        if all_done:
            assignment = db.session.get(DeliveryAssignment, stop.assignment_id)
            if assignment and assignment.status != "delivered":
                assignment.status = "delivered"
                if trip:
                    trip.status        = "completed"
                    trip.arrival_time  = now
                    if trip.departure_time:
                        dur = (now - trip.departure_time).total_seconds() / 60.0
                        trip.duration_minutes = round(dur, 1)
                        trip.is_on_time = dur <= 60.0
                else:
                    new_trip = DeliveryTrip(
                        assignment_id=stop.assignment_id, emp_id=emp_id,
                        trip_date=date.today(), arrival_time=now, status="completed",
                    )
                    db.session.add(new_trip)

        db.session.commit()

        if all_done:
            try:
                compute_delivery_badges(emp_id)
            except Exception:
                pass
            log_audit("delivery_complete", str(emp_id),
                      f"All {total} stops done for assignment #{stop.assignment_id}")

        return jsonify({"ok": True, "done": done, "total": total,
                        "all_done": all_done,
                        "minutes_from_prev": stop.minutes_from_prev})
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_log_stop: {e}")
        return jsonify({"ok": False, "error": "Server error"}), 500


# ─── Phone alerts (Web Push) ────────────────────────────────────────────────
@app.route("/sw.js")
def service_worker():
    """Served from the root so it may show alerts for the whole site."""
    r = app.send_static_file("sw.js")
    r.headers["Service-Worker-Allowed"] = "/"
    r.headers["Cache-Control"] = "no-cache"
    r.mimetype = "application/javascript"
    return r


@app.route("/push/key")
@login_required
def push_key():
    return jsonify({"key": vapid_keys()[0]})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    d = request.get_json(silent=True) or {}
    sub = d.get("subscription") or {}
    endpoint = str(sub.get("endpoint") or "")[:600]
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"ok": False, "message": "That browser did not send a valid subscription."}), 400
    emp_id = session.get("user_id")
    PushSub.query.filter_by(endpoint=endpoint).delete()      # one owner per device
    db.session.add(PushSub(emp_id=emp_id, endpoint=endpoint, p256dh=str(keys["p256dh"])[:200],
                           auth=str(keys["auth"])[:100]))
    db.session.commit()
    return jsonify({"ok": True, "message": "Phone alerts are on."})


@app.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    d = request.get_json(silent=True) or {}
    PushSub.query.filter_by(emp_id=session.get("user_id"), endpoint=str(d.get("endpoint") or "")).delete()
    db.session.commit()
    return jsonify({"ok": True, "message": "Phone alerts are off on this device."})


@app.route("/push/test", methods=["POST"])
@login_required
def push_test():
    n = push_now(session.get("user_id"), "Test alert", "Phone alerts work on this device.", "/dashboard")
    return jsonify({"ok": n > 0, "message": "Test alert sent." if n else "No device with alerts on."})


# ─── Bill-based picking: routes ─────────────────────────────────────────────
def _pick_role():
    return session.get("staff_type", ""), bool(session.get("is_admin"))


def _pick_reply(ok, msg, code=200):
    """JSON for the live screens; a flash and redirect for plain form posts."""
    if request.is_json or request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": ok, "message": msg}), (200 if ok else code)
    flash(msg, "success" if ok else "warning")
    return redirect(url_for("dashboard"))


@app.route("/pick/state.json")
@login_required
def pick_state():
    role, is_admin = _pick_role()
    if role not in ("picker", "pick_assigner") and not is_admin:
        return jsonify({"ok": False, "message": "Not available for your role."}), 403
    emp = db.session.get(Employee, session.get("user_id"))
    if role == "picker" and emp and (not emp.last_seen_at or datetime.utcnow() - emp.last_seen_at > timedelta(seconds=60)):
        emp.last_seen_at = datetime.utcnow()
        db.session.commit()
    return jsonify(picking_state(emp, "picker" if role == "picker" else "pick_assigner"))


@app.route("/pick/bill", methods=["POST"])
@login_required
def pick_add_bill():
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can add bills.", 403)
    data = request.get_json(silent=True) or request.form
    try:
        b = add_pick_bill(data.get("bill_no"), data.get("items"), session.get("user_id"))
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Bill %s added: %d items. Free pickers notified." % (b.bill_no, b.items))


@app.route("/pick/claim", methods=["POST"])
@login_required
def pick_claim():
    role, _ = _pick_role()
    if role != "picker":
        return _pick_reply(False, "Only pickers can take items.", 403)
    data = request.get_json(silent=True) or request.form
    try:
        c = claim_items(session.get("user_id"), int(data.get("bill_id") or 0), data.get("items"))
    except (PickError, ValueError) as exc:
        return _pick_reply(False, str(exc) if isinstance(exc, PickError) else "Choose a bill.", 400)
    mins = c.items * PICK_SECONDS_PER_ITEM / 60.0
    return _pick_reply(True, "%d item%s taken. You have %s minutes." % (
        c.items, "" if c.items == 1 else "s", ("%g" % round(mins, 1))))


@app.route("/pick/done", methods=["POST"])
@login_required
def pick_done():
    role, _ = _pick_role()
    if role != "picker":
        return _pick_reply(False, "Only pickers can finish items.", 403)
    data = request.get_json(silent=True) or request.form
    missing = data.get("missing") or []
    if not isinstance(missing, list):
        missing = []
    try:
        missing = [(m.get("item"), int(m.get("qty") or 0)) for m in missing if isinstance(m, dict)]
        c, completed = finish_claim(session.get("user_id"), int(data.get("claim_id") or 0), missing=missing)
    except (PickError, ValueError, TypeError) as exc:
        return _pick_reply(False, str(exc) if isinstance(exc, PickError) else "Nothing to finish.", 400)
    pts = ("%g" % c.points)
    if c.outcome == "ontime":
        msg = "In normal time: +%s points." % pts
    elif c.outcome in ("extra", "report"):
        msg = "Done in extra time: +%s points." % pts
    else:
        msg = "Late: %s points." % pts
    if c.missing:
        msg += " %d missing item%s reported to the assigner." % (c.missing, "" if c.missing == 1 else "s")
    if completed:
        msg += " Bill complete."
    return _pick_reply(True, msg)


@app.route("/pick/bill/<int:bill_id>/cancel", methods=["POST"])
@login_required
def pick_cancel_bill(bill_id):
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can cancel bills.", 403)
    try:
        cancel_pick_bill(bill_id)
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Bill cancelled.")


@app.route("/pick/missing/<int:missing_id>/<action>", methods=["POST"])
@login_required
def pick_missing_action(missing_id, action):
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can handle missing items.", 403)
    try:
        handle_missing(missing_id, action)
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Items put back on the bill for another picker." if action == "reassign"
                       else "Marked as not available.")


@app.route("/pick/bill/<int:bill_id>.json")
@login_required
def pick_bill_detail(bill_id):
    role, is_admin = _pick_role()
    if role not in ("picker", "pick_assigner") and not is_admin:
        return jsonify({"ok": False, "message": "Not available for your role."}), 403
    try:
        return jsonify(bill_timeline(bill_id))
    except PickError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404


@app.route("/pick/bill/<int:bill_id>/reschedule", methods=["POST"])
@login_required
def pick_reschedule(bill_id):
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can reschedule bills.", 403)
    data = request.get_json(silent=True) or request.form
    try:
        local = datetime.strptime("%s %s" % (data.get("date", ""), data.get("time", "")), "%Y-%m-%d %H:%M")
        reschedule_bill(bill_id, local - IST)
    except ValueError:
        return _pick_reply(False, "Choose a date and a time.", 400)
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Bill rescheduled for %s." % local.strftime("%d %b, %I:%M %p").replace(" 0", " "))


@app.route("/pick/bill/<int:bill_id>/bring_back", methods=["POST"])
@login_required
def pick_bring_back(bill_id):
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can do that.", 403)
    try:
        b = bring_back_bill(bill_id)
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Bill %s is open again." % b.bill_no)


@app.route("/pick/close_day", methods=["POST"])
@login_required
def pick_close_day():
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can close the day.", 403)
    try:
        d, moved = close_pick_day(session.get("user_id"))
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    msg = "Day closed: %d bills. Checkers' day total is now fixed at %d." % (d.total_bills, d.total_bills)
    if moved:
        msg += " %d unfinished bill%s moved to Rescheduled (9:00 AM tomorrow)." % (moved, "" if moved == 1 else "s")
    return _pick_reply(True, msg)


@app.route("/pick/claim/<int:claim_id>/release", methods=["POST"])
@login_required
def pick_release_claim(claim_id):
    role, is_admin = _pick_role()
    if role != "pick_assigner" and not is_admin:
        return _pick_reply(False, "Only the Pick Assigner can release items.", 403)
    try:
        release_claim(claim_id)
    except PickError as exc:
        return _pick_reply(False, str(exc), 400)
    return _pick_reply(True, "Items released back to the bill.")


@app.route("/notifications")
@login_required
def notifications_list():
    """JSON feed for the in-app bell."""
    emp_id = session.get("user_id")
    try:
        notes = (Notification.query.filter_by(emp_id=emp_id)
                 .order_by(Notification.created_at.desc()).limit(20).all())
        unread = sum(1 for n in notes if not n.is_read)
        return jsonify({"ok": True, "unread": unread, "items": [
            {"id": n.id, "title": n.title, "body": n.body, "link": n.link,
             "is_read": bool(n.is_read),
             "ago": n.created_at.strftime("%d %b %H:%M") if n.created_at else ""}
            for n in notes
        ]})
    except Exception as e:
        logger.error(f"notifications_list: {e}")
        return jsonify({"ok": False, "items": [], "unread": 0})


@app.route("/notifications/read", methods=["POST"])
@login_required
def notifications_mark_read():
    emp_id = session.get("user_id")
    try:
        Notification.query.filter_by(emp_id=emp_id, is_read=False).update({"is_read": True})
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"notifications_mark_read: {e}")
        return jsonify({"ok": False}), 500


# ─── SUPERVISOR: authoritative bill counts + 5-strike point penalty ───────────

# Tunable: points removed per mismatch (scaled by the size of the gap).
SUPERVISOR_STRIKE_FREE = 5          # first 5 mismatches per month are free
SUPERVISOR_PENALTY_PER_BILL = 0.5   # points per bill of difference, beyond the free strikes
SUPERVISOR_PENALTY_CAP = 15.0       # never deduct more than this in a month


def _self_reported_bills(emp_id, d):
    """Best-effort: a staff member's own reported bill count for a given date."""
    e = KPIEntry.query.filter_by(emp_id=emp_id, entry_date=d).first()
    if e is not None:
        # Prefer an explicit bills field if present, else fall back to picked/checked volume
        for attr in ("total_bills_received", "bills_received", "picked", "checked"):
            v = getattr(e, attr, None)
            if v:
                return int(v)
        return 0
    n = DeliveryBillerNote.query.filter_by(emp_id=emp_id, entry_date=d).first()
    if n is not None:
        return int(n.quantity or 0)
    return None  # nothing reported that day


def supervisor_strike_summary(emp_id, month_str):
    """Returns dict: mismatches this month, penalty points (scaled, after 5 free), and details.
    Only counts days the supervisor entered an admin-validated super_bills value."""
    try:
        rows = (SupervisorBill.query
                .filter(SupervisorBill.staff_id == emp_id,
                        SupervisorBill.admin_validated == True)  # noqa: E712
                .all())
        mismatches = []
        for r in rows:
            if not r.entry_date or r.entry_date.strftime("%Y-%m") != month_str:
                continue
            reported = _self_reported_bills(emp_id, r.entry_date)
            if reported is None:
                continue
            gap = abs(int(r.super_bills or 0) - int(reported))
            if gap > 0:
                mismatches.append({"date": r.entry_date, "super": r.super_bills,
                                   "reported": reported, "gap": gap})
        mismatches.sort(key=lambda m: m["date"])
        n = len(mismatches)
        penalty = 0.0
        # Deduct only for mismatches beyond the free allowance, scaled by gap
        for m in mismatches[SUPERVISOR_STRIKE_FREE:]:
            penalty += m["gap"] * SUPERVISOR_PENALTY_PER_BILL
        penalty = round(min(penalty, SUPERVISOR_PENALTY_CAP), 1)
        return {"mismatches": n, "free": SUPERVISOR_STRIKE_FREE,
                "penalty": penalty, "details": mismatches}
    except Exception as e:
        logger.warning(f"supervisor_strike_summary: {e}")
        return {"mismatches": 0, "free": SUPERVISOR_STRIKE_FREE, "penalty": 0.0, "details": []}


def checklist_for(emp_id, staff_type, on_date=None):
    """Return this employee's checklist for a date: [(template, done_bool)].

    Driven by the live templates for their staff_type, so an item the admin adds
    today appears for everyone in that role immediately, and an item they
    archive disappears without destroying past completion history.
    """
    on_date = on_date or date.today()
    try:
        tmpls = (ChecklistTemplate.query
                 .filter_by(staff_type=staff_type, is_active=1)
                 .order_by(ChecklistTemplate.sort_order, ChecklistTemplate.id)
                 .all())
        if not tmpls:
            return []
        done_ids = {c.template_id for c in ChecklistCompletion.query.filter_by(
            emp_id=emp_id, entry_date=on_date).all() if c.done}
        return [(t, t.id in done_ids) for t in tmpls]
    except Exception as e:
        logger.warning(f"checklist_for({emp_id}, {staff_type}): {e}")
        return []


def checklist_pct(emp_id, staff_type, on_date=None):
    """Percentage of today's checklist this employee has ticked (0.0 when none)."""
    items = checklist_for(emp_id, staff_type, on_date)
    if not items:
        return None          # None means "no checklist defined for this role"
    done = sum(1 for _t, d in items if d)
    return round(done / len(items) * 100, 1)


@app.route("/admin/checklist", methods=["GET", "POST"])
@admin_required
def admin_checklist():
    """Admin defines the daily checklist items for each staff type."""
    try:
        if request.method == "POST":
            action = (request.form.get("action", "") or "").strip()
            if action == "add":
                role = (request.form.get("staff_type", "") or "").strip()
                text = (request.form.get("text", "") or "").strip()[:200]
                if role not in VALID_STAFF_TYPES:
                    flash("Pick a valid staff type.", "warning")
                elif not text:
                    flash("Enter the task text.", "warning")
                else:
                    nxt = db.session.query(db.func.coalesce(
                        db.func.max(ChecklistTemplate.sort_order), 0)).filter_by(
                        staff_type=role).scalar() or 0
                    db.session.add(ChecklistTemplate(
                        staff_type=role, text=text, sort_order=nxt + 1,
                        created_by=session.get("user_id")))
                    db.session.commit()
                    log_audit("checklist_add", role, text)
                    flash(f"Task added for {ROLE_DISPLAY_NAMES.get(role, role.title())}.", "success")
            elif action == "archive":
                tid = int(request.form.get("template_id", 0) or 0)
                row = db.session.get(ChecklistTemplate, tid)
                if row:
                    row.is_active = 0
                    db.session.commit()
                    log_audit("checklist_archive", row.staff_type, row.text)
                    flash("Task removed from the daily list.", "success")
            elif action == "restore":
                tid = int(request.form.get("template_id", 0) or 0)
                row = db.session.get(ChecklistTemplate, tid)
                if row:
                    row.is_active = 1
                    db.session.commit()
                    flash("Task restored.", "success")
            elif action == "move":
                # Swap with the adjacent task in the same role, then renumber the
                # whole list. The previous version just nudged sort_order by one
                # without touching the neighbour, so two tasks ended up sharing a
                # value, the order silently fell back to id, and the buttons
                # looked like they did nothing.
                tid = int(request.form.get("template_id", 0) or 0)
                direction = (request.form.get("dir", "") or "").strip()
                row = db.session.get(ChecklistTemplate, tid)
                if row and direction in ("up", "down"):
                    sibs = (ChecklistTemplate.query
                            .filter_by(staff_type=row.staff_type, is_active=1)
                            .order_by(ChecklistTemplate.sort_order,
                                      ChecklistTemplate.id).all())
                    idx = next((i for i, t in enumerate(sibs) if t.id == row.id), None)
                    swap = (idx - 1) if direction == "up" else (idx + 1)
                    if idx is not None and 0 <= swap < len(sibs):
                        sibs[idx], sibs[swap] = sibs[swap], sibs[idx]
                        for i, t in enumerate(sibs, start=1):
                            t.sort_order = i      # 1..n, no ties, no gaps
                        db.session.commit()
            return redirect(url_for("admin_checklist"))

        today = date.today()
        by_role, archived_by_role = {}, {}
        for role in VALID_STAFF_TYPES:
            by_role[role] = (ChecklistTemplate.query
                             .filter_by(staff_type=role, is_active=1)
                             .order_by(ChecklistTemplate.sort_order, ChecklistTemplate.id).all())
            archived_by_role[role] = (ChecklistTemplate.query
                                      .filter_by(staff_type=role, is_active=0)
                                      .order_by(ChecklistTemplate.id).all())
        # Today's completion, grouped by role so a manager can see at a glance
        # which jobs are clear and which still have work outstanding, rather
        # than scanning one flat alphabetical list.
        completion_by_role = []
        for role in VALID_STAFF_TYPES:
            role_rows = []
            for emp in (Employee.query.filter_by(is_admin=False, staff_type=role)
                        .order_by(Employee.name).all()):
                items = checklist_for(emp.id, emp.staff_type, today)
                if not items:
                    continue
                done = sum(1 for _t, d in items if d)
                role_rows.append({
                    "emp": emp,
                    "total": len(items),
                    "done": done,
                    "pct": round(done / len(items) * 100),
                    "is_done": done == len(items),
                    "not_started": done == 0,
                    "pending": [t.text for t, d in items if not d],
                    "completed": [t.text for t, d in items if d],
                })
            if not role_rows:
                continue
            finished = sum(1 for r in role_rows if r["is_done"])
            completion_by_role.append({
                "role": role,
                "role_name": ROLE_DISPLAY_NAMES.get(role, role.title()),
                "rows": role_rows,
                "staff_count": len(role_rows),
                "finished": finished,
                "outstanding": len(role_rows) - finished,
                "tasks": len(by_role.get(role, [])),
                "all_clear": finished == len(role_rows),
            })

        # Flat list kept for the overall counters.
        staff_rows = [r for g in completion_by_role for r in g["rows"]]
        return render_template("admin_checklist.html",
                               by_role=by_role, archived_by_role=archived_by_role,
                               staff_rows=staff_rows,
                               completion_by_role=completion_by_role,
                               today=today,
                               valid_roles=VALID_STAFF_TYPES,
                               role_names=ROLE_DISPLAY_NAMES)
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_checklist: {e}")
        flash("Error loading the checklist manager.", "danger")
        return redirect(url_for("admin_dashboard"))

@app.route("/checklist/toggle", methods=["POST"])
@login_required
def checklist_toggle():
    """Staff ticks or un-ticks one of today's checklist items."""
    emp_id = session.get("user_id")
    staff_type = session.get("staff_type", "")
    try:
        data = request.get_json(silent=True) or {}
        tid = int(data.get("template_id", 0) or 0)
        want = bool(data.get("done"))
        tmpl = db.session.get(ChecklistTemplate, tid)
        # Only an item belonging to the role the employee is currently working.
        if not tmpl or tmpl.is_active != 1 or tmpl.staff_type != staff_type:
            return jsonify({"ok": False, "error": "That task is not on your list."}), 403
        today = date.today()
        row = ChecklistCompletion.query.filter_by(
            emp_id=emp_id, template_id=tid, entry_date=today).first()
        if not row:
            row = ChecklistCompletion(emp_id=emp_id, template_id=tid, entry_date=today)
            db.session.add(row)
        row.done = 1 if want else 0
        if want and not row.completed_at:
            row.completed_at = datetime.utcnow()
        db.session.commit()
        items = checklist_for(emp_id, staff_type, today)
        return jsonify({"ok": True, "done": bool(row.done),
                        "total": len(items),
                        "completed": sum(1 for _t, d in items if d)})
    except Exception as e:
        db.session.rollback()
        logger.error(f"checklist_toggle: {e}")
        return jsonify({"ok": False, "error": "Could not save."}), 500


@app.route("/api/palette")
@login_required
def api_palette():
    """Everything the command palette can jump to, scoped to who is asking.

    Admin-only destinations and the staff directory are withheld from
    non-admins, so the palette can never become a way to enumerate colleagues.
    """
    try:
        is_admin = bool(session.get("is_admin"))
        staff_type = session.get("staff_type", "")
        me = session.get("user_id")
        items = []

        if is_admin:
            items += [
                {"t": "Admin Dashboard",   "s": "Scores, staff table, reports", "u": "/admin_dashboard",     "g": "Go to"},
                {"t": "Daily Checklists",  "s": "Define tasks per role",        "u": "/admin/checklist",     "g": "Go to"},
                {"t": "Delivery Dashboard","s": "Routes, trips, GPS trails",    "u": "/admin/delivery",      "g": "Go to"},
                {"t": "Delivery Leaderboard","s": "Rider rankings",             "u": "/delivery/leaderboard","g": "Go to"},
                {"t": "Supervisor Console","s": "Authoritative bill counts",    "u": "/supervisor",          "g": "Go to"},
                {"t": "Reports Archive",   "s": "Past monthly reports",         "u": "/admin/reports",       "g": "Go to"},
                {"t": "Bulk Import",       "s": "Add staff from CSV",           "u": "/admin/bulk_import",   "g": "Go to"},
                {"t": "Export CSV",        "s": "Download all KPI data",        "u": "/admin/export_csv",    "g": "Action"},
                {"t": "Export JSON",       "s": "Full data export",             "u": "/admin/export_full",   "g": "Action"},
            ]
            for e in Employee.query.filter_by(is_admin=False).order_by(Employee.name).all():
                role = ROLE_DISPLAY_NAMES.get(e.staff_type, (e.staff_type or "").title())
                sub = role + (" + " + e.secondary_staff_type.title() if e.secondary_staff_type else "")
                items.append({"t": e.name, "s": sub, "u": "/staff/%d" % e.id, "g": "Staff"})
        else:
            items.append({"t": "My Dashboard", "s": "Today's entry and stats", "u": "/dashboard", "g": "Go to"})
            if me:
                items.append({"t": "My Profile", "s": "My scores and history", "u": "/staff/%d" % me, "g": "Go to"})
            if staff_type == "supervisor" or session.get("secondary_staff_type") == "supervisor":
                items.append({"t": "Supervisor Console", "s": "Bill counts", "u": "/supervisor", "g": "Go to"})
            if staff_type == "delivery":
                items.append({"t": "Delivery Leaderboard", "s": "Rider rankings", "u": "/delivery/leaderboard", "g": "Go to"})

        items.append({"t": "Log out", "s": "End this session", "u": "/logout", "g": "Action"})
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        logger.error(f"api_palette: {e}")
        return jsonify({"ok": False, "items": []}), 500


@app.route("/supervisor", methods=["GET", "POST"])
@login_required
def supervisor_console():
    """Supervisor enters the authoritative ('super') bill count per staff per day."""
    staff_type = session.get("staff_type", "")
    is_super = (staff_type == "supervisor" or session.get("secondary_staff_type") == "supervisor"
                or session.get("is_admin"))
    if not is_super:
        flash("Supervisor access only.", "danger")
        return redirect(url_for("dashboard"))
    try:
        today = date.today()

        # Supervisor can ALSO multitask — same engine as the staff dashboard
        if request.method == "POST" and request.form.get("multitask_only") == "1":
            try:
                n = _save_multitask_entry(session.get("user_id"), today, "supervisor")
                if n:
                    flash("Multitask role report saved.", "success")
                else:
                    flash("Nothing to save — tick a role and fill at least one number.", "warning")
            except Exception as mte:
                db.session.rollback()
                logger.warning(f"Supervisor multitask save: {mte}")
                flash("Error saving multitask report.", "danger")
            return redirect(url_for("supervisor_console"))

        if request.method == "POST":
            sid = int(request.form.get("staff_id", 0) or 0)
            d_raw = (request.form.get("entry_date", "") or "").strip()
            try:
                d = datetime.strptime(d_raw, "%Y-%m-%d").date() if d_raw else today
            except ValueError:
                d = today
            bills = max(0, int(request.form.get("super_bills", 0) or 0))
            note = (request.form.get("note", "") or "").strip()[:200]
            if sid:
                row = SupervisorBill.query.filter_by(staff_id=sid, entry_date=d).first()
                if not row:
                    row = SupervisorBill(staff_id=sid, entry_date=d)
                    db.session.add(row)
                row.super_bills = bills
                row.supervisor_id = session.get("user_id")
                row.note = note
                # New/changed value needs admin re-validation
                row.admin_validated = bool(session.get("is_admin"))
                db.session.commit()
                flash(f"Saved super-bills for {d.strftime('%d %b')}.", "success")
            return redirect(url_for("supervisor_console"))

        staff = Employee.query.filter_by(is_admin=False).order_by(Employee.name).all()
        recent = (SupervisorBill.query.order_by(SupervisorBill.entry_date.desc())
                  .limit(60).all())
        emp_names = {e.id: e.name for e in staff}
        # Supervisor's own multitask reports for today (shown on the console)
        try:
            today_multitask = (MultitaskEntry.query
                               .filter_by(emp_id=session.get("user_id"), entry_date=today)
                               .order_by(MultitaskEntry.created_at.desc()).all())
        except Exception:
            today_multitask = []
        return render_template("supervisor.html", staff=staff, recent=recent,
                               emp_names=emp_names, today=today,
                               is_admin=bool(session.get("is_admin")),
                               user_name=session.get("user_name", "Supervisor"),
                               today_multitask=today_multitask,
                               primary_staff_type=session.get("primary_staff_type"),
                               secondary_staff_type=session.get("secondary_staff_type"))
    except Exception as e:
        logger.error(f"supervisor_console: {e}")
        flash("Error loading supervisor console.", "danger")
        return redirect(url_for("dashboard"))


@app.route("/admin/validate_super_bill/<int:row_id>", methods=["POST"])
@admin_required
def admin_validate_super_bill(row_id):
    """Admin confirms a supervisor's bill value is the true value."""
    try:
        row = db.session.get(SupervisorBill, row_id)
        if row:
            row.admin_validated = True
            db.session.commit()
            flash("Super-bill validated.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"admin_validate_super_bill: {e}")
        flash("Error validating.", "danger")
    return redirect(request.referrer or url_for("admin_dashboard"))


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


@app.route("/delivery/breadcrumb/<int:trip_id>", methods=["POST"])
@login_required
def delivery_breadcrumb(trip_id):
    """Save a GPS breadcrumb point for an active trip.

    Location is the most sensitive thing this system holds, so three limits
    apply. Points are accepted only while the trip is actually in progress, so
    a finished trip cannot keep reporting where someone is. Points closer than
    GPS_MIN_SECONDS or GPS_MIN_METRES to the last one are discarded, because a
    browser emits a fix every few seconds and storing all of them is a far more
    detailed record of a person's movements than delivery tracking needs. And a
    single trip cannot exceed GPS_MAX_POINTS.
    """
    emp_id = session.get("user_id")
    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = float(data.get("lat", 0))
        lng = float(data.get("lng", 0))
        if not lat and not lng:
            return jsonify({"ok": False, "reason": "no_coords"})
        trip = db.session.get(DeliveryTrip, trip_id)
        if not trip or trip.emp_id != emp_id:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        # Tracking stops when the trip does.
        if (trip.status or "") != "in_progress" or trip.arrival_time is not None:
            return jsonify({"ok": False, "reason": "trip_not_active"}), 409

        last = (BreadcrumbPoint.query.filter_by(trip_id=trip_id)
                .order_by(BreadcrumbPoint.recorded_at.desc()).first())
        if last:
            if BreadcrumbPoint.query.filter_by(trip_id=trip_id).count() >= GPS_MAX_POINTS:
                return jsonify({"ok": False, "reason": "cap_reached"}), 429
            if last.recorded_at:
                gap = (datetime.utcnow() - last.recorded_at).total_seconds()
                if gap < GPS_MIN_SECONDS:
                    return jsonify({"ok": True, "stored": False, "reason": "too_soon"})
            if _haversine_m(lat, lng, last.lat, last.lng) < GPS_MIN_METRES:
                return jsonify({"ok": True, "stored": False, "reason": "too_close"})

        db.session.add(BreadcrumbPoint(trip_id=trip_id, lat=lat, lng=lng))
        db.session.commit()
        return jsonify({"ok": True, "stored": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"delivery_breadcrumb: {e}")
        return jsonify({"ok": False, "reason": "server_error"}), 500


@app.route("/admin/delivery/breadcrumbs/<int:trip_id>")
@admin_required
def admin_delivery_breadcrumbs(trip_id):
    """Return all breadcrumb points for a trip (admin-only)."""
    try:
        pts = (BreadcrumbPoint.query
               .filter_by(trip_id=trip_id)
               .order_by(BreadcrumbPoint.recorded_at)
               .all())
        return jsonify([{"lat": p.lat, "lng": p.lng,
                         "t": p.recorded_at.strftime("%H:%M")} for p in pts])
    except Exception as e:
        logger.error(f"admin_delivery_breadcrumbs: {e}")
        return jsonify([])


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


@app.route("/admin/debug_delivery_staff")
@admin_required
def admin_debug_delivery_staff():
    """Diagnostic — lists every employee and their primary/secondary roles."""
    try:
        emps = Employee.query.all()
        rows = []
        delivery_count = 0
        for e in emps:
            primary = (getattr(e, "staff_type", "") or "").lower()
            secondary = (getattr(e, "secondary_staff_type", "") or "").lower()
            is_delivery = (primary == "delivery" or secondary == "delivery") and not getattr(e, "is_admin", False)
            if is_delivery:
                delivery_count += 1
            rows.append({
                "id": e.id, "name": e.name, "email": e.email,
                "is_admin": bool(getattr(e, "is_admin", False)),
                "primary": primary, "secondary": secondary,
                "is_delivery_eligible": is_delivery,
            })
        return jsonify({
            "total_employees": len(emps),
            "delivery_eligible_count": delivery_count,
            "employees": rows,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

        # ── Journey map date: ?day=YYYY-MM-DD lets admin replay any past day ──
        map_day = today
        _day_raw = (request.args.get("day", "") or "").strip()
        if _day_raw:
            try:
                map_day = datetime.strptime(_day_raw, "%Y-%m-%d").date()
            except ValueError:
                map_day = today

        # ── Stops for journey map (selected day's assignments) ──
        day_aids = [a.id for a in assignments if a.assigned_at and a.assigned_at.date() == map_day]
        day_stops = (DeliveryStop.query
                     .filter(DeliveryStop.assignment_id.in_(day_aids))
                     .order_by(DeliveryStop.assignment_id, DeliveryStop.id).all()
                     if day_aids else [])
        stops_map = {}
        for st in day_stops:
            stops_map.setdefault(st.assignment_id, []).append(st)

        # Map: rider name -> list of trip_ids (for breadcrumb loading, selected day)
        rider_trip_ids = {}
        for a in assignments:
            if a.assigned_at and a.assigned_at.date() == map_day:
                t = trip_map.get(a.id)
                if t:
                    rider = emp_map.get(a.delivery_emp_id)
                    rname = rider.name if rider else "Unknown"
                    rider_trip_ids.setdefault(rname, [])
                    if t.id not in rider_trip_ids[rname]:
                        rider_trip_ids[rname].append(t.id)

        # Every date that has at least one assignment — for the history picker
        delivery_days = sorted({a.assigned_at.date() for a in assignments if a.assigned_at},
                               reverse=True)

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
                               in_transit_count=in_transit_count,
                               stops_map=stops_map,
                               rider_trip_ids=rider_trip_ids,
                               map_day=map_day,
                               delivery_days=delivery_days,
                               store_lat=STORE_LAT,
                               store_lng=STORE_LNG)
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
    # Privacy: expire GPS location trails nightly, and once on boot so the
    # retention window is honoured even if the service rarely stays up overnight.
    _scheduler.add_job(
        purge_old_location_data,
        trigger="cron",
        hour=3, minute=20,
        id="purge_location_data",
        replace_existing=True
    )
    _scheduler.add_job(
        run_late_pick_alerts,
        trigger="interval", minutes=1,
        id="run_late_pick_alerts",
        replace_existing=True
    )
    _scheduler.add_job(
        run_level_checks,
        trigger="cron",
        hour=3, minute=50,
        id="run_level_checks",
        replace_existing=True
    )
    _scheduler.add_job(
        auto_raise_targets,
        trigger="cron",
        day=1, hour=8, minute=10,
        id="auto_raise_targets",
        replace_existing=True
    )
    if not _scheduler.running:
        _scheduler.start()
    try:
        purge_old_location_data()
    except Exception as _pe:
        logger.warning(f"startup location purge skipped: {_pe}")
    logger.info("APScheduler started — monthly reports job scheduled.")
except Exception as _sch_err:
    logger.warning(f"APScheduler init failed: {_sch_err}")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
