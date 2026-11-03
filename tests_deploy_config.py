"""Deployment configuration: mail reporting honestly, and diagnostics.

The bug this pins down: send_monthly_reports() returned None whether it had
sent or silently given up because mail was unconfigured, and the manual trigger
flashed "Monthly reports sent" either way. The admin was told mail worked while
nothing left the server.

Run:  python3.13 tests_deploy_config.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import app, db, Employee  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []

MAIL_VARS = ["MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD", "ADMIN_EMAIL_RECIPIENT"]


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


def clear_mail_env():
    for v in MAIL_VARS:
        os.environ.pop(v, None)


def set_mail_env():
    os.environ["MAIL_SERVER"] = "smtp.example.com"
    os.environ["MAIL_USERNAME"] = "reports@example.com"
    os.environ["MAIL_PASSWORD"] = "app-password"
    os.environ["ADMIN_EMAIL_RECIPIENT"] = "owner@example.com"


with app.app_context():
    db.create_all()
    e = Employee.query.filter_by(email="dc_adm@x.com").first()
    if not e:
        e = Employee(name="DC Admin", email="dc_adm@x.com",
                     staff_type="picker", is_admin=True)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()

client = csrf_client(app.test_client())
client.post("/login", data={"email": "dc_adm@x.com", "password": PW},
            follow_redirects=True)

# ---- 1. Unconfigured mail reports failure, not success ------------------
clear_mail_env()
missing = A.mail_config_status()
check("1.1 All four mail vars reported missing", set(missing) == set(MAIL_VARS))

ok, note = A.send_monthly_reports()
check("1.2 send_monthly_reports returns a status pair", isinstance(ok, bool))
check("1.3 Unconfigured mail reports failure", ok is False)
check("1.4 The message names what is missing", "MAIL_SERVER" in note)

r = client.get("/admin/send_monthly_report", follow_redirects=True)
check("1.5 Manual trigger no longer claims success",
      b"Monthly reports sent" not in r.data)
check("1.6 Manual trigger says mail is not configured",
      b"not configured" in r.data.lower())

r = client.post("/admin/test_mail", follow_redirects=True)
check("1.7 Test email refuses when unconfigured", b"not configured" in r.data.lower())

# ---- 2. Configured mail passes the config gate --------------------------
set_mail_env()
check("2.1 Nothing reported missing once set", A.mail_config_status() == [])
ok, note = A.send_monthly_reports()
check("2.2 Past the config gate, failure is now the mail server, not config",
      ok is False and "Missing:" not in note)

# ---- 3. Diagnostics page ------------------------------------------------
clear_mail_env()
r = client.get("/admin/diagnostics")
d = r.data
check("3.1 Diagnostics page loads", r.status_code == 200)
check("3.2 Lists the problems found", b"Problems Found" in d)
check("3.3 Flags unconfigured mail", b"Mail is not configured" in d)
check("3.4 Flags the default admin password", b"admin123" in d)
check("3.5 Flags the geofence being off", b"geofence" in d.lower())
check("3.6 Shows every mail variable", all(v.encode() in d for v in MAIL_VARS))
check("3.7 Shows the delivery variables",
      b"STORE_LAT" in d and b"STORE_LNG" in d)
check("3.8 Shows the scoring targets",
      b"PICKER_TARGET_HR" in d and b"CHECKER_TARGET_HR" in d)
check("3.9 Reports database connectivity", b"Database" in d)
check("3.10 Explains where to set them", b"Environment tab" in d)

# ---- 4. Staff must not reach any of it ----------------------------------
with app.app_context():
    s = Employee.query.filter_by(email="dc_staff@x.com").first()
    if not s:
        s = Employee(name="DC Staff", email="dc_staff@x.com", staff_type="picker")
        s.set_password(PW)
        db.session.add(s)
        db.session.commit()
client.get("/logout")
client.post("/login", data={"email": "dc_staff@x.com", "password": PW},
            follow_redirects=True)
r = client.get("/admin/diagnostics", follow_redirects=True)
check("4.1 Staff cannot open diagnostics", b"Problems Found" not in r.data)
r = client.post("/admin/test_mail", follow_redirects=True)
check("4.2 Staff cannot trigger a test email", b"Test email sent" not in r.data)

# ---- 5. render.yaml declares what the code reads ------------------------
import re  # noqa: E402
import yaml  # noqa: E402

with open(os.path.join(REPO, "render.yaml")) as f:
    spec = yaml.safe_load(f)
declared = {v["key"] for v in spec["services"][0]["envVars"]}
src = open(os.path.join(REPO, "app.py")).read()
read_by_code = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))

check("5.1 render.yaml is valid YAML", isinstance(spec, dict))
check("5.2 No Render-managed database is provisioned", "databases" not in spec)
check("5.3 DATABASE_URL stays out of the repo",
      any(v["key"] == "DATABASE_URL" and v.get("sync") is False
          for v in spec["services"][0]["envVars"]))
undeclared = sorted(read_by_code - declared)
check("5.4 Every env var the code reads is declared (%s)"
      % (", ".join(undeclared) if undeclared else "none missing"),
      not undeclared)
check("5.5 Mail variables are declared", set(MAIL_VARS).issubset(declared))
check("5.6 Single worker, so the monthly job cannot double-fire",
      "-w 1" in spec["services"][0]["startCommand"])


# ---- 6. The app boots the way Render runs it ---------------------------
# Three deploys failed in a row with no app-code change behind them: relaxing
# the version pins let SQLAlchemy reach 2.1, which moved the default driver for
# a bare "postgresql://" URL from psycopg2 to psycopg (v3). Only psycopg2-binary
# is shipped, so the app died at boot. These assert the driver is named
# explicitly rather than left to a default that can move.
import subprocess  # noqa: E402

def _boot(url):
    """Import the app in a fresh process with DATABASE_URL set."""
    code = ("import os,sys;"
            "os.environ['DATABASE_URL']=%r;os.environ['RENDER']='true';"
            "sys.path.insert(0,%r);"
            "from app import app;"
            "print(app.config['SQLALCHEMY_DATABASE_URI'].split('://')[0])" % (url, REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    return r.returncode, (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "", r.stderr

rc, scheme, err = _boot("postgresql://u:p@localhost:5432/d")
if rc:
    print("   boot stderr:", err[-300:])
check("6.1 App imports with a PostgreSQL URL set", rc == 0)
check("6.2 The driver is named explicitly, not left to a default (%s)" % scheme,
      scheme in ("postgresql+psycopg2", "postgresql+psycopg"))

rc2, scheme2, err2 = _boot("postgres://u:p@localhost:5432/d")
check("6.3 The legacy postgres:// scheme still works", rc2 == 0)
check("6.4 Legacy scheme also gets an explicit driver", scheme2 in ("postgresql+psycopg2", "postgresql+psycopg"))

check("6.5 A PostgreSQL driver is actually in requirements.txt",
      any(x in open(os.path.join(REPO, "requirements.txt")).read().lower()
          for x in ("psycopg2", "psycopg[")))


# ---- 8. Mail fails fast and says why -----------------------------------
# The "Send test email" button hung for about two minutes and then gave up.
# Flask-Mail opens the SMTP socket with no timeout, so a wrong host or a
# blocked port stalls rather than erroring, and with one worker that blocks the
# whole service until gunicorn kills the request. These assert it now gives up
# quickly with an actionable message.
import time as _time  # noqa: E402

os.environ["MAIL_SERVER"] = "10.255.255.1"      # routable but never answers
os.environ["MAIL_PORT"] = "587"
os.environ["MAIL_USERNAME"] = "x@example.com"
os.environ["MAIL_PASSWORD"] = "x"
os.environ["ADMIN_EMAIL_RECIPIENT"] = "owner@example.com"

_t0 = _time.time()
_ok, _msg = A.send_test_email("owner@example.com")
_elapsed = _time.time() - _t0

check("8.1 An unreachable mail host fails rather than hanging", _ok is False)
check("8.2 It gives up within roughly the timeout, not minutes (%.0fs)" % _elapsed,
      _elapsed < A.MAIL_TIMEOUT + 12)
check("8.3 The message is actionable, not a bare traceback",
      any(w in _msg.lower() for w in ("timed out", "could not reach", "blocked")))
check("8.4 The message names the host and port", "10.255.255.1" in _msg)

import socket as _socket  # noqa: E402
check("8.5 The timeout is scoped, not left on globally",
      _socket.getdefaulttimeout() is None)
with A.mail_socket_timeout():
    _inside = _socket.getdefaulttimeout()
check("8.6 Inside the context the timeout applies", _inside == A.MAIL_TIMEOUT)
check("8.7 It is restored afterwards", _socket.getdefaulttimeout() is None)

os.environ["MAIL_SERVER"] = "no-such-host.invalid"
_ok2, _msg2 = A.send_test_email("owner@example.com")
check("8.8 An unresolvable host is reported clearly",
      _ok2 is False and "could not reach" in _msg2.lower())

clear_mail_env()

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
