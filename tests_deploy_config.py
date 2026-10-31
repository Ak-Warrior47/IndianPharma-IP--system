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

client = app.test_client()
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

clear_mail_env()
print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
