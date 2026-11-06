"""Live error log: failures are recorded, counted, shown to the admin.

The app used to send a crashed request to the login page and many routes
swallow their own errors, so a failure looked like "the button does nothing"
and nobody could see why. Every error the app logs is now recorded with the
page, the user and the reason, shown on Diagnostics, and the owner is emailed
the first time each new one happens.

Run:  python3.13 tests_error_log.py
"""
import os
import sys
from datetime import date, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import app, db, Employee, ErrorLog, Announcement  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


# Routes that fail on purpose. Registered before the first request.
@app.route("/__test_boom")
def _boom():
    return 1 / 0


@app.route("/__test_swallow")
def _swallow():
    A.logger.error("Dashboard error: test swallowed failure")
    return "ok"


@app.route("/__test_halfsave")
def _halfsave():
    db.session.add(Announcement(title="HALF-SAVED-SHOULD-NOT-EXIST", body="x"))
    db.session.flush()
    raise RuntimeError("failed after a half-finished change")


with app.app_context():
    db.create_all()
    ErrorLog.query.delete()
    Announcement.query.filter_by(title="HALF-SAVED-SHOULD-NOT-EXIST").delete()
    for em, adm in (("el_adm@x.com", True), ("el_staff@x.com", False)):
        e = Employee.query.filter_by(email=em).first()
        if not e:
            e = Employee(name="EL " + em[:5], email=em, staff_type="picker", is_admin=adm)
            e.set_password(PW)
            db.session.add(e)
    db.session.commit()

c = csrf_client(app.test_client())
c.post("/login", data={"email": "el_adm@x.com", "password": PW}, follow_redirects=True)

r = c.get("/__test_boom")
check("1.1 A crash still lands the user somewhere sensible", r.status_code in (302, 303))
with app.app_context():
    rows = ErrorLog.query.filter_by(path="/__test_boom").all()
check("1.2 The crash is recorded", len(rows) == 1)
check("1.3 With the reason", rows and "division" in (rows[0].message + rows[0].detail).lower(),
      rows[0].message if rows else "")
check("1.4 With the full technical detail", rows and "ZeroDivisionError" in rows[0].detail)
check("1.5 With who it happened to", rows and rows[0].user_name.startswith("EL"))
r2 = c.get("/__test_boom", follow_redirects=True)
check("1.6 The user is told something went wrong", b"Something went wrong" in r2.data)
with app.app_context():
    rows = ErrorLog.query.filter_by(path="/__test_boom").all()
check("1.7 A repeat is counted, not duplicated", len(rows) == 1 and rows[0].count == 2,
      "%d rows, count %s" % (len(rows), rows[0].count if rows else None))

c.get("/__test_swallow")
with app.app_context():
    sw = ErrorLog.query.filter_by(path="/__test_swallow").first()
check("2.1 Errors a page catches and hides are recorded too", sw is not None and "swallowed" in sw.message)

c.get("/__test_halfsave")
with app.app_context():
    leaked = Announcement.query.filter_by(title="HALF-SAVED-SHOULD-NOT-EXIST").first()
    hs = ErrorLog.query.filter_by(path="/__test_halfsave").first()
check("3.1 The failed page's half-finished change is NOT saved by the logging", leaked is None)
check("3.2 And the failure itself is recorded", hs is not None)

d = c.get("/admin/diagnostics").data
check("4.1 Diagnostics lists recent errors", b"Recent Errors" in d and b"/__test_boom" in d)
check("4.2 Diagnostics counts them as a problem", b"different error(s) happened" in d)
check("4.3 The technical detail is one click away", b"technical detail" in d)

c.get("/logout")
s = csrf_client(app.test_client())
s.post("/login", data={"email": "el_staff@x.com", "password": PW}, follow_redirects=True)
check("5.1 Staff cannot see the error list",
      b"Recent Errors" not in s.get("/admin/diagnostics", follow_redirects=True).data)

with app.app_context():
    db.session.add(ErrorLog(day=date.today() - timedelta(days=45), message="old", where="x", path="/old"))
    db.session.commit()
    A.purge_old_location_data()
    check("6.1 Errors older than 30 days are cleared nightly",
          ErrorLog.query.filter_by(path="/old").first() is None)
    check("6.2 Recent ones are kept", ErrorLog.query.filter_by(path="/__test_boom").first() is not None)
    ErrorLog.query.delete()
    for em in ("el_adm@x.com", "el_staff@x.com"):
        e = Employee.query.filter_by(email=em).first()
        if e:
            db.session.delete(e)
    db.session.commit()

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
