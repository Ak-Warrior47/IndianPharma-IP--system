"""Bill-based picking: the owner's new picker system.

A Pick Assigner adds a bill (bill number + items). Free pickers are notified
and grab items from it themselves; several pickers share one bill. Each grab
has a timer of items x PICK_SECONDS_PER_ITEM. Done on time: +1 per item.
Late: -1 per started minute over, never more than -5. The picker score comes
from these grabs.

The owner's example: a 10-item bill, picker 1 takes 3, picker 2 takes 3,
picker 4 takes 2, picker 1 takes 1 more -> 9 of 10 picked, 1 left.

Run:  python3.13 tests_picker_v2.py
"""
import os
import sys
from datetime import date, datetime, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import (app, db, Employee, KPIEntry, Notification, PickBill, PickClaim,  # noqa: E402
                 PersonalRecord, LevelEvent)
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
SEC = A.PICK_SECONDS_PER_ITEM
EMAILS = ["pk_asg@x.com", "pk_p1@x.com", "pk_p2@x.com", "pk_p3@x.com", "pk_p4@x.com"]


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def mk(email, st):
    e = Employee.query.filter_by(email=email).first()
    if not e:
        e = Employee(name="PK " + email.split("@")[0].split("_")[1].upper(), email=email, staff_type=st)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    e.staff_type = st
    for M in (PickClaim, KPIEntry, Notification, PersonalRecord, LevelEvent):
        col = M.picker_id if M is PickClaim else M.emp_id
        M.query.filter(col == e.id).delete(synchronize_session=False)
    db.session.commit()
    return e


def login(email):
    c = csrf_client(app.test_client())
    c.post("/login", data={"email": email, "password": PW}, follow_redirects=True)
    return c


def jpost(c, url, body=None):
    r = c.post(url, json=body or {}, headers={"X-Requested-With": "fetch"})
    return r.status_code, (r.get_json() or {})


with app.app_context():
    db.create_all()
    PickClaim.query.delete()
    PickBill.query.delete()
    db.session.commit()
    asg = mk("pk_asg@x.com", "pick_assigner")
    p1, p2, p3, p4 = (mk(e, "picker") for e in EMAILS[1:])
    ids = {"p1": p1.id, "p2": p2.id, "p3": p3.id, "p4": p4.id}
    # other pickers left in the shared test database must not be "busy" here
    others = [e.id for e in Employee.query.filter(Employee.staff_type == "picker").all() if e.id not in ids.values()]

# ---- 1. Roles and pages -----------------------------------------------------
check("1.1 Pick Assigner is a valid role", "pick_assigner" in A.VALID_STAFF_TYPES)
check("1.2 It is named 'Pick Assigner'", A.ROLE_DISPLAY_NAMES.get("pick_assigner") == "Pick Assigner")
ca = login("pk_asg@x.com")
dash = ca.get("/dashboard").data.decode()
check("1.3 The assigner's dashboard shows the Add bill form", 'id="pkAdd"' in dash and "Pick Assigner" in dash)
c1, c2, c3, c4 = (login(e) for e in EMAILS[1:])
pdash = c1.get("/dashboard").data.decode()
check("1.4 The picker's dashboard shows the picking screen", 'id="pkBills"' in pdash)
check("1.5 The old picker daily form is gone", 'name="total_bills_received"' not in pdash
      and "Submit Today's Metrics" not in pdash)
r = c1.post("/dashboard", data={"picked": "100", "total_bills_received": "10"}, follow_redirects=True)
with app.app_context():
    check("1.6 A picker cannot post old daily totals",
          KPIEntry.query.filter_by(emp_id=ids["p1"], entry_date=date.today()).first() is None)
check("1.7 Admin add-employee form offers Pick Assigner",
      'value="pick_assigner"' in open(os.path.join(REPO, "templates", "admin.html")).read())

# ---- 2. Adding a bill notifies free pickers --------------------------------
code, j = jpost(c1, "/pick/bill", {"bill_no": "X1", "items": 5})
check("2.1 A picker cannot add bills", code == 403)
code, j = jpost(ca, "/pick/bill", {"bill_no": "4521", "items": 10})
check("2.2 The assigner adds bill 4521 with 10 items", code == 200 and j.get("ok"), str(j))
with app.app_context():
    bill = PickBill.query.filter_by(bill_no="4521").first()
    BID = bill.id
    check("2.3 Every free picker is notified",
          all(Notification.query.filter_by(emp_id=i).filter(Notification.title.like("%4521%")).count() == 1
              for i in ids.values()))
code, j = jpost(ca, "/pick/bill", {"bill_no": "4521", "items": 3})
check("2.4 The same open bill number cannot be added twice", code == 400)
code, j = jpost(ca, "/pick/bill", {"bill_no": "B0", "items": 0})
check("2.5 A bill needs at least 1 item", code == 400)

# ---- 3. The owner's example: several pickers share one bill -----------------
code, j = jpost(c1, "/pick/claim", {"bill_id": BID, "items": 3})
check("3.1 Picker 1 takes 3 items", code == 200 and j.get("ok"), str(j))
code, j = jpost(c1, "/pick/claim", {"bill_id": BID, "items": 1})
check("3.2 One grab at a time: picker 1 must finish first", code == 400 and "Finish" in j.get("message", ""))
jpost(c2, "/pick/claim", {"bill_id": BID, "items": 3})
jpost(c4, "/pick/claim", {"bill_id": BID, "items": 2})
code, j = jpost(c3, "/pick/claim", {"bill_id": BID, "items": 5})
check("3.3 Nobody can take more than is left (2 left, asked 5)", code == 400 and "Only 2" in j.get("message", ""))
with app.app_context():
    pr = A.pick_bill_progress(db.session.get(PickBill, BID))
    check("3.4 3 + 3 + 2 being picked, 2 left", pr["active"] == 8 and pr["left"] == 2, str(pr))
    busy = {e.id for e in A.free_pickers()}
    check("3.5 Busy pickers are not listed as free; picker 3 is", ids["p3"] in busy
          and not ({ids["p1"], ids["p2"], ids["p4"]} & busy))

for c in (c1, c2, c4):
    code, j = jpost(c, "/pick/done", {"claim_id": 0})   # wrong id
with app.app_context():
    mine = {cl.picker_id: cl.id for cl in PickClaim.query.filter_by(bill_id=BID, status="active").all()}
code, j = jpost(c1, "/pick/done", {"claim_id": mine[ids["p1"]]})
check("3.6 Picker 1 finishes on time: +3 points", code == 200 and "+3" in j.get("message", ""), str(j))
jpost(c2, "/pick/done", {"claim_id": mine[ids["p2"]]})
jpost(c4, "/pick/done", {"claim_id": mine[ids["p4"]]})
code, j = jpost(c1, "/pick/claim", {"bill_id": BID, "items": 1})
check("3.7 Picker 1 takes 1 more after finishing", code == 200)
with app.app_context():
    pr = A.pick_bill_progress(db.session.get(PickBill, BID))
    check("3.8 The owner's example: 8 picked + 1 being picked, 1 left", pr["done"] == 8 and pr["active"] == 1
          and pr["left"] == 1, str(pr))
    c1id = PickClaim.query.filter_by(picker_id=ids["p1"], status="active").first().id
jpost(c1, "/pick/done", {"claim_id": c1id})
code, j = jpost(c3, "/pick/claim", {"bill_id": BID, "items": 1})
with app.app_context():
    c3id = PickClaim.query.filter_by(picker_id=ids["p3"], status="active").first().id
code, j = jpost(c3, "/pick/done", {"claim_id": c3id})
check("3.9 The last item completes the bill", "Bill complete" in j.get("message", ""), str(j))
with app.app_context():
    b = db.session.get(PickBill, BID)
    check("3.10 The bill is marked complete with a completion time", b.status == "done" and b.completed_at is not None)

# ---- 4. Timer and points -----------------------------------------------------
t0 = datetime(2026, 1, 5, 10, 0, 0)
due = t0 + timedelta(seconds=3 * SEC)
check("4.1 Timer is items x time per item (3 items = %d s)" % (3 * SEC), True)
check("4.2 On time: +1 per item", A.pick_points(3, due, due - timedelta(seconds=1)) == 3)
check("4.3 Exactly at the deadline still counts as on time", A.pick_points(3, due, due) == 3)
check("4.4 90 s late = 2 started minutes: -2", A.pick_points(3, due, due + timedelta(seconds=90)) == -2)
check("4.5 9 minutes late is capped at -5", A.pick_points(3, due, due + timedelta(minutes=9)) == -5)
with app.app_context():
    db.session.add(PickBill(bill_no="T9", items=4))
    db.session.commit()
    tb = PickBill.query.filter_by(bill_no="T9").first()
    cl = A.claim_items(ids["p2"], tb.id, 2, now=t0)
    check("4.6 A grab's deadline is set from its size", cl.due_at == t0 + timedelta(seconds=2 * SEC))
    cl, _ = A.finish_claim(ids["p2"], cl.id, now=cl.due_at + timedelta(minutes=3), day=date.today())
    check("4.7 Finished 3 minutes late: -3 points", cl.points == -3, str(cl.points))

# ---- 5. Assigner tools ---------------------------------------------------------
code, j = jpost(ca, "/pick/bill", {"bill_no": "R1", "items": 4})
with app.app_context():
    rb = PickBill.query.filter_by(bill_no="R1").first().id
jpost(c4, "/pick/claim", {"bill_id": rb, "items": 4})
with app.app_context():
    rc = PickClaim.query.filter_by(picker_id=ids["p4"], status="active").first().id
code, j = jpost(ca, "/pick/claim/%d/release" % rc)
with app.app_context():
    check("5.1 The assigner can release a stuck grab; its items go back",
          code == 200 and A.pick_bill_progress(db.session.get(PickBill, rb))["left"] == 4)
code, j = jpost(ca, "/pick/bill/%d/cancel" % rb)
with app.app_context():
    check("5.2 The assigner can cancel a bill", db.session.get(PickBill, rb).status == "cancelled")
s = ca.get("/pick/state.json").get_json()
check("5.3 The assigner sees every picker, free or busy", s and all(any(p["id"] == i for p in s["pickers"])
                                                                    for i in ids.values()))
s = c1.get("/pick/state.json").get_json()
check("5.4 A picker sees today's items and points", s and s["today"]["items"] == 4 and s["today"]["points"] == 4,
      str(s and s.get("today")))

# ---- 6. Score and daily record -------------------------------------------------
with app.app_context():
    e = KPIEntry.query.filter_by(emp_id=ids["p1"], entry_date=date.today()).first()
    check("6.1 A finished grab keeps the picker's day record in step (4 items, 1 bill)",
          e is not None and e.picked == 4 and e.sales_bills_open == 1, str(e and (e.picked, e.sales_bills_open)))
    st = A.build_analytics([e], "picker", emp_id=ids["p1"])
    names = [r["name"] for r in st["point_breakdown"]]
    check("6.2 The picker score is On time 40 / Speed 30 / Volume 20 / Consistency 10",
          names == ["On time", "Pick speed", "Volume", "Consistency"]
          and [r["max"] for r in st["point_breakdown"]] == [40, 30, 20, 10], str(names))
    ot = st["point_breakdown"][0]
    check("6.3 All grabs on time earns the full 40", ot["got"] == 40.0, str(ot))
    check("6.4 The figures are returned for the screens", st["pick_stats"]["items"] == 4)
    e2 = KPIEntry.query.filter_by(emp_id=ids["p2"], entry_date=date.today()).first()
    st2 = A.build_analytics([e2], "picker", emp_id=ids["p2"])
    check("6.5 One late grab of two halves the On-time points", st2["point_breakdown"][0]["got"] == 20.0,
          str(st2["point_breakdown"][0]))
    check("6.6 Level-ups use items per hour actually picking",
          A._level_day_value("picker", e) == st["pick_stats"]["daily_rate"][date.today()])

# ---- 7. Old screens are closed for pickers ---------------------------------------
r = c1.get("/past_entry/%s" % (date.today() - timedelta(days=1)).isoformat(), follow_redirects=True)
check("7.1 Past-date entry is closed for pickers", b"recorded live" in r.data)

# ---- 8. Trial copy: test accounts and banner, never on the live database -----
TRIAL = ["ravi@pharmaip.com", "lisa@pharmaip.com", "smriti@pharmaip.com", "deepti@pharmaip.com",
         "amit@pharmaip.com"]
with app.app_context():
    pre = {em for em in TRIAL if Employee.query.filter_by(email=em).first()}
    os.environ.pop("TRIAL_DEMO", None)
    check("8.1 No test accounts without TRIAL_DEMO", A.seed_trial_demo() == 0)
    os.environ["TRIAL_DEMO"] = "1"
    os.environ["DATABASE_URL"] = "postgresql://live.example/db"
    check("8.2 Never with a live DATABASE_URL set", A.seed_trial_demo() == 0)
    os.environ.pop("DATABASE_URL")
    A.seed_trial_demo()
    ravi = Employee.query.filter_by(email="ravi@pharmaip.com").first()
    check("8.3 TRIAL_DEMO creates the Pick Assigner and four pickers",
          ravi is not None and ravi.staff_type == "pick_assigner"
          and all(Employee.query.filter_by(email=em).first() for em in TRIAL))
    check("8.4 Running it again adds nobody twice", A.seed_trial_demo() == 0
          and Employee.query.filter_by(email="lisa@pharmaip.com").count() == 1)
tc = app.test_client()
check("8.5 Every page shows the trial banner", b"Trial copy: not the live app" in tc.get("/login").data)
os.environ.pop("TRIAL_DEMO")
check("8.6 No banner on a normal (live) copy", b"Trial copy" not in tc.get("/login").data)
with app.app_context():
    for em in TRIAL:
        if em not in pre:
            e = Employee.query.filter_by(email=em).first()
            if e:
                db.session.delete(e)
    db.session.commit()

with app.app_context():
    for em in EMAILS:
        e = Employee.query.filter_by(email=em).first()
        if e:
            for M in (KPIEntry, Notification, PersonalRecord, LevelEvent):
                M.query.filter_by(emp_id=e.id).delete()
            PickClaim.query.filter_by(picker_id=e.id).delete()
            db.session.delete(e)
    PickBill.query.delete()
    db.session.commit()

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
