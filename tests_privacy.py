"""Privacy: location minimisation, retention, and a staff member's own data.

The claims this app now makes to staff have to be true, so each one is pinned:
tracking stops when the trip does, points are thinned rather than stored at
browser rate, the trail expires, personal email stays out of the logs, and one
person's export can never contain another's.

Run:  python3.13 tests_privacy.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import json  # noqa: E402
import logging  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402

import app as A  # noqa: E402
from app import (  # noqa: E402
    app, db, Employee, DeliveryAssignment, DeliveryTrip, BreadcrumbPoint,
    KPIEntry, redact_email, purge_old_location_data, location_data_summary,
)

from tests_helper import csrf_client  # noqa: E402
app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + detail) if detail and not cond else ""))


with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False):
        e = Employee.query.filter_by(email=email).first()
        if e:
            return e
        e = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin,
                     phone="+919812345670", sunday_override=True)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
        return e

    mk("PV Admin", "pv_adm@x.com", "picker", admin=True)
    rider = mk("PV Rider", "pv_rider@x.com", "delivery")
    other = mk("PV Other", "pv_other@x.com", "delivery")
    biller = mk("PV Biller", "pv_bil@x.com", "biller")
    rider_id, other_id, biller_id = rider.id, other.id, biller.id

    a = DeliveryAssignment(purchaser_id=biller.id, delivery_emp_id=rider_id,
                           packet_type="Box", status="in_transit",
                           destination_addr="Cuttack", destination_lat=20.47,
                           destination_lng=85.89, package_desc="2 boxes",
                           bills_count=2, assigned_at=datetime.utcnow())
    db.session.add(a)
    db.session.flush()
    trip = DeliveryTrip(assignment_id=a.id, emp_id=rider_id, trip_date=date.today(),
                        departure_time=datetime.utcnow(), status="in_progress")
    db.session.add(trip)
    db.session.commit()
    trip_id = trip.id

client = csrf_client(app.test_client())


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


def crumb(tid, lat, lng):
    return client.post("/delivery/breadcrumb/%d" % tid, json={"lat": lat, "lng": lng})


# ---- 1. Email stays out of the logs ------------------------------------
_r = redact_email("arjun.mehta@gmail.com")
check("1.1 Email is redacted for logging",
      _r.startswith("a") and _r.endswith("@gmail.com")
      and "rjun.mehta" not in _r and set(_r.split("@")[0][1:]) == {"*"}, _r)
check("1.2 Empty address does not leak", redact_email("") == "(none)")
check("1.3 Malformed address is still masked", "@" not in redact_email("notanemail"))

cap = []


class Grab(logging.Handler):
    def emit(self, rec):
        cap.append(rec.getMessage())


logging.getLogger("app").addHandler(Grab())
logging.getLogger("app").setLevel(logging.INFO)
cap.clear()
login("pv_rider@x.com")
joined = " ".join(cap)
check("1.4 A successful login does not log the full address",
      "pv_rider@x.com" not in joined, joined[:120])
cap.clear()
client.get("/logout")
client.post("/login", data={"email": "pv_rider@x.com", "password": "wrong"},
            follow_redirects=True)
check("1.5 A failed login does not log the full address",
      "pv_rider@x.com" not in " ".join(cap))

# ---- 2. Location is minimised at capture -------------------------------
login("pv_rider@x.com")
r = crumb(trip_id, 20.4600, 85.8800)
check("2.1 First point of an active trip is stored",
      (r.get_json() or {}).get("stored") is True)

r = crumb(trip_id, 20.46001, 85.88001)          # ~1 metre away
body = r.get_json() or {}
check("2.2 A point a metre away is discarded, not stored", body.get("stored") is False)
check("2.3 The reason given is distance or interval",
      body.get("reason") in ("too_close", "too_soon"), str(body))

with app.app_context():
    stored = BreadcrumbPoint.query.filter_by(trip_id=trip_id).count()
check("2.4 Only the meaningful point was written", stored == 1)

# ---- 3. Tracking stops when the trip does ------------------------------
with app.app_context():
    t = db.session.get(DeliveryTrip, trip_id)
    t.status = "completed"
    t.arrival_time = datetime.utcnow()
    db.session.commit()
r = crumb(trip_id, 20.5000, 85.9500)
check("3.1 A finished trip refuses further location points", r.status_code == 409)
check("3.2 The refusal says the trip is not active",
      (r.get_json() or {}).get("reason") == "trip_not_active")
with app.app_context():
    check("3.3 Nothing was written after the trip ended",
          BreadcrumbPoint.query.filter_by(trip_id=trip_id).count() == 1)

# ---- 4. One rider cannot report another's location ---------------------
login("pv_other@x.com")
r = crumb(trip_id, 20.47, 85.89)
check("4.1 A rider cannot post points to someone else's trip", r.status_code == 404)

client.get("/logout")
r = crumb(trip_id, 20.47, 85.89)
check("4.2 A logged-out request cannot post location",
      r.status_code in (301, 302, 401, 403))

# ---- 5. Retention actually expires the trail ---------------------------
with app.app_context():
    old_a = DeliveryAssignment(purchaser_id=biller_id, delivery_emp_id=rider_id,
                               packet_type="Box", status="completed",
                               destination_addr="Cuttack", destination_lat=20.40,
                               destination_lng=85.80, package_desc="1 box",
                               bills_count=1,
                               assigned_at=datetime.utcnow() - timedelta(days=90))
    db.session.add(old_a)
    db.session.flush()
    old_trip = DeliveryTrip(assignment_id=old_a.id, emp_id=rider_id,
                            trip_date=date.today() - timedelta(days=90),
                            departure_time=datetime.utcnow() - timedelta(days=90),
                            status="completed")
    db.session.add(old_trip)
    db.session.flush()
    for i in range(5):
        db.session.add(BreadcrumbPoint(
            trip_id=old_trip.id, lat=20.4 + i / 1000, lng=85.8 + i / 1000,
            recorded_at=datetime.utcnow() - timedelta(days=A.GPS_RETENTION_DAYS + 10)))
    db.session.add(BreadcrumbPoint(trip_id=old_trip.id, lat=20.49, lng=85.88,
                                   recorded_at=datetime.utcnow()))
    db.session.commit()
    before = BreadcrumbPoint.query.count()

deleted, cutoff = purge_old_location_data()
with app.app_context():
    after = BreadcrumbPoint.query.count()
    stale = (BreadcrumbPoint.query
             .filter(BreadcrumbPoint.recorded_at <
                     datetime.utcnow() - timedelta(days=A.GPS_RETENTION_DAYS)).count())
check("5.1 Expired location points are deleted", deleted == 5, "deleted=%s" % deleted)
check("5.2 Nothing older than the retention window survives", stale == 0)
check("5.3 Recent points are kept", after == before - 5 and after > 0)
check("5.4 A second purge deletes nothing more", purge_old_location_data()[0] == 0)
check("5.5 Retention is reported for the admin",
      location_data_summary()["retention_days"] == A.GPS_RETENTION_DAYS)

# ---- 6. A person can see and take their own data -----------------------
login("pv_rider@x.com")
r = client.get("/my-data")
d = r.data
check("6.1 My Data page loads", r.status_code == 200)
check("6.2 It states tracking happens only during a trip",
      b"Only during a delivery trip" in d)
check("6.3 It states how long location is kept",
      str(A.GPS_RETENTION_DAYS).encode() in d and b"Deleted automatically" in d)
check("6.4 It states who can see it", b"Seen only by you and the admin" in d)
check("6.5 It says refusing location still allows the trip",
      b"the trip still works" in d)
check("6.6 It says the password cannot be read back",
      b"one-way hash" in d)
check("6.7 It lists the record types held", b"GPS location points" in d
      and b"Daily KPI entries" in d)

r = client.get("/my-data/download")
check("6.8 Download returns a file",
      r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", ""))
payload = json.loads(r.data.decode())
check("6.9 Export is about the right person",
      payload["about"]["email"] == "pv_rider@x.com")
check("6.10 Export carries no password hash",
      b"password_hash" not in r.data and b"pbkdf2" not in r.data and b"scrypt" not in r.data)
blob = json.dumps(payload)
check("6.11 Export contains no other employee's address",
      "pv_other@x.com" not in blob and "pv_adm@x.com" not in blob)
check("6.12 Export explains the password omission", "one-way hash" in blob)

client.get("/logout")
r = client.get("/my-data", follow_redirects=True)
check("6.13 My Data requires a login", b"Only during a delivery trip" not in r.data)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
