"""Bill-based picking: the owner's new picker system.

A Pick Assigner adds a bill (bill number + items). Free pickers are notified
and grab items from it themselves; several pickers share one bill. Each grab
has a timer of items x 30 s (20 s urgent), then 125 s extra: half to find,
half to report only. Normal time earns 1.5 points per item, extra time 1 per
item, late -1 per started minute, never more than -5. Bill numbers get the
financial-year prefix (IP2627/) and can never be reused.

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
from app import (app, db, Employee, KPIEntry, Notification, PickBill, PickClaim, PickEvent,  # noqa: E402
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
    ASG_ID = asg.id
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
    bill = PickBill.query.filter_by(bill_no=A.fy_prefix() + "4521").first()
    BID = bill.id
    check("2.3 Every free picker is notified",
          all(Notification.query.filter_by(emp_id=i).filter(Notification.title.like("%4521%")).count() == 1
              for i in ids.values()))
code, j = jpost(ca, "/pick/bill", {"bill_no": "4521", "items": 3})
check("2.4 The same bill number cannot be entered twice", code == 400 and "only once" in j.get("message", ""))
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
check("3.6 Picker 1 finishes in normal time: 1.5 x 3 = +4.5 points", code == 200 and "+4.5" in j.get("message", ""), str(j))
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
check("4.1 Normal time is 30 s per item", SEC == 30)
check("4.2 Done in normal time: 1.5 per item", A.pick_points(3, due, due - timedelta(seconds=1)) == 4.5)
check("4.3 Exactly at the end of normal time still counts", A.pick_points(3, due, due) == 4.5)
check("4.4 Extra time (find): 1 per item, no cut", A.pick_points(3, due, due + timedelta(seconds=60)) == 3.0)
check("4.4b Extra time (report): still 1 per item", A.pick_points(3, due, due + timedelta(seconds=120)) == 3.0)
check("4.4c 90 s after the 125 s extra time = 2 started minutes late: -2",
      A.pick_points(3, due, due + timedelta(seconds=125 + 90)) == -2)
check("4.5 Very late is capped at -5", A.pick_points(3, due, due + timedelta(minutes=30)) == -5)
with app.app_context():
    db.session.add(PickBill(bill_no="T9", items=4))
    db.session.commit()
    tb = PickBill.query.filter_by(bill_no="T9").first()
    cl = A.claim_items(ids["p2"], tb.id, 2, now=t0)
    check("4.6 A grab's deadline is set from its size", cl.due_at == t0 + timedelta(seconds=2 * SEC))
    cl, _ = A.finish_claim(ids["p2"], cl.id, now=cl.due_at + timedelta(minutes=3), day=date.today())
    check("4.7 Finished 3 minutes after normal time (55 s after extra time): -1", cl.points == -1, str(cl.points))

# ---- 5. Assigner tools ---------------------------------------------------------
code, j = jpost(ca, "/pick/bill", {"bill_no": "990001", "items": 4})
with app.app_context():
    rb = PickBill.query.filter_by(bill_no=A.fy_prefix() + "990001").first().id
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
check("5.4 A picker sees today's items and points (4.5 + 1.5)", s and s["today"]["items"] == 4 and s["today"]["points"] == 6,
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

# ---- 7b. The assigner is alerted when a picker runs late -------------------
with app.app_context():
    Notification.query.filter_by(emp_id=ASG_ID).delete()
    db.session.add(PickBill(bill_no="L1", items=5))
    db.session.commit()
    lb = PickBill.query.filter_by(bill_no="L1").first()
    LB_ID = lb.id
    t0 = datetime.utcnow() - timedelta(minutes=20)
    lc = A.claim_items(ids["p3"], lb.id, 3, now=t0)          # timer ran out ~15 min ago
    ok_c = A.claim_items(ids["p4"], lb.id, 1)               # still within its timer
    n = A.alert_late_picks()
    msgs = Notification.query.filter_by(emp_id=ASG_ID).all()
    check("7b.1 The assigner is told who is late, on which bill",
          n == 1 and len(msgs) == 1 and "PK P3 is late on bill L1" in msgs[0].title, str([m.title for m in msgs]))
    check("7b.2 A picker still within the timer is not reported",
          not any("P4" in m.title for m in msgs))
    check("7b.3 Each late grab alerts only once", A.alert_late_picks() == 0
          and Notification.query.filter_by(emp_id=ASG_ID).count() == 1)
s_late = ca.get("/pick/state.json").get_json()
row = next((r for r in s_late["pickers"] if r["id"] == ids["p3"]), {})
check("7b.4 The assigner's screen lists the late picker with how late",
      row.get("late_by", 0) > 600 and "late_by" not in next(r for r in s_late["pickers"] if r["id"] == ids["p4"]))
check("7b.5 The screen has the red running-late box", 'id="pkLate"' in ca.get("/dashboard").data.decode())
with app.app_context():
    for cl in PickClaim.query.filter_by(bill_id=LB_ID).all():
        cl.status = "cancelled"
    db.session.commit()

# ---- 9. Owner's second round ------------------------------------------------
from datetime import date as _date  # noqa: E402
check("9.1 Prefix follows the financial year: Nov 2026 -> IP2627/", A.fy_prefix(_date(2026, 11, 8)) == "IP2627/")
check("9.2 31 Mar 2027 is still IP2627/, 1 Apr 2027 becomes IP2728/",
      A.fy_prefix(_date(2027, 3, 31)) == "IP2627/" and A.fy_prefix(_date(2027, 4, 1)) == "IP2728/")
code, j = jpost(ca, "/pick/bill", {"bill_no": "1234567", "items": 3})
check("9.3 Bill number is at most 6 digits", code == 400)
code, j = jpost(ca, "/pick/bill", {"bill_no": "12ab", "items": 3})
check("9.4 Letters other than a final U are refused", code == 400)
with app.app_context():
    # earlier sections leave bills open; close them so urgency is tested alone
    for b in PickBill.query.filter_by(status="open").all():
        b.status = "cancelled"
    for c in PickClaim.query.filter_by(status="active").all():
        c.status = "cancelled"
    db.session.commit()
jpost(ca, "/pick/bill", {"bill_no": "5001", "items": 4})
code, j = jpost(ca, "/pick/bill", {"bill_no": "5002u", "items": 4})
with app.app_context():
    nb = PickBill.query.filter_by(bill_no=A.fy_prefix() + "5001").first()
    ub = PickBill.query.filter_by(bill_no=A.fy_prefix() + "5002").first()
    NB, UB = nb.id, ub.id
    check("9.5 U on the end marks the bill urgent and the prefix is added", ub is not None and ub.is_urgent and not nb.is_urgent)
code, j = jpost(c1, "/pick/claim", {"bill_id": NB, "items": 2})
check("9.6 Urgent bills come first: a normal bill can't be taken while urgent items wait",
      code == 400 and "Urgent" in j.get("message", ""), str(j))
code, j = jpost(c1, "/pick/claim", {"bill_id": UB, "items": 3})
with app.app_context():
    uc = PickClaim.query.filter_by(picker_id=ids["p1"], status="active").first()
    check("9.7 Urgent items get 20 s each (3 items = 60 s)",
          uc is not None and abs((uc.due_at - uc.started_at).total_seconds() - 60) < 1)
    UC = uc.id
jpost(c2, "/pick/claim", {"bill_id": UB, "items": 1})
with app.app_context():
    c2c = PickClaim.query.filter_by(picker_id=ids["p2"], status="active").first().id
code, j = jpost(c1, "/pick/done", {"claim_id": UC})
jpost(c2, "/pick/done", {"claim_id": c2c})
with app.app_context():
    cl1, cl2 = db.session.get(PickClaim, UC), db.session.get(PickClaim, c2c)
    check("9.8 The urgent pool (10) is shared by items picked: 7.5 and 2.5",
          cl1.bonus == 7.5 and cl2.bonus == 2.5, "%s %s" % (cl1.bonus, cl2.bonus))
s9 = c1.get("/pick/state.json").get_json()
row = next(b for b in s9["bills"] if b["id"] == NB)
check("9.9 Times are shown in 12-hour form", row["added"].endswith(("AM", "PM")), row["added"])
code, j = jpost(c3, "/pick/claim", {"bill_id": NB, "items": 4})
with app.app_context():
    c3c = PickClaim.query.filter_by(picker_id=ids["p3"], status="active").first().id
code, j = jpost(c3, "/pick/done", {"claim_id": c3c, "missing": [{"item": "Dolo 650", "qty": 1}, {"item": "Pan 40", "qty": 1}]})
check("9.10 A picker can report missing items: 2 picked, 2 not found", code == 200 and "2 missing" in j.get("message", ""), str(j))
with app.app_context():
    cl = db.session.get(PickClaim, c3c)
    pr = A.pick_bill_progress(db.session.get(PickBill, NB))
    check("9.11 Picked and missing are recorded on the grab", cl.picked == 2 and cl.missing == 2)
    check("9.12 Picked items in normal time: 1.5 x 2 = 3; missing items wait for the assigner",
          cl.points == 3.0, str(cl.points))
    check("9.13 The bill shows 2 missing and is not complete", pr["missing"] == 2 and db.session.get(PickBill, NB).status == "open")
    check("9.14 The assigner is notified about the missing items",
          Notification.query.filter_by(emp_id=ASG_ID).filter(Notification.title.like("Missing items%")).count() >= 1)
sa = ca.get("/pick/state.json").get_json()
brow = next(b for b in sa["bills"] if b["id"] == NB)
check("9.15 The assigner sees which items, who reported them and when",
      len(brow["missing_rows"]) == 2 and brow["missing_rows"][0]["by"] == "PK P3" and brow["missing_rows"][0]["at"])
check("9.16 'Taken by' shows each picker and their items", any(t["name"] == "PK P3" and t["items"] == 2 for t in brow["taken"]))
check("9.17 The day's late report lists the missing items", any(e["kind"] == "missing" and "Dolo 650" in e["detail"] for e in sa["events"]))
mids = [m["id"] for m in brow["missing_rows"]]
code, j = jpost(c1, "/pick/missing/%d/reassign" % mids[0])
check("9.18 Only the assigner can handle missing items", code == 403)
jpost(ca, "/pick/missing/%d/reassign" % mids[0])
with app.app_context():
    pr = A.pick_bill_progress(db.session.get(PickBill, NB))
    check("9.19 Reassign puts the item back on the bill for another picker", pr["left"] == 1 and pr["missing"] == 1, str(pr))
jpost(ca, "/pick/missing/%d/unavailable" % mids[1])
code, j = jpost(c4, "/pick/claim", {"bill_id": NB, "items": 1})
with app.app_context():
    c4c = PickClaim.query.filter_by(picker_id=ids["p4"], status="active").first().id
jpost(c4, "/pick/done", {"claim_id": c4c})
with app.app_context():
    check("9.20 Once the reassigned item is picked and the other marked not available, the bill completes",
          db.session.get(PickBill, NB).status == "done")
    check("9.20b The confirmed-missing item pays its reporter (3 + 1.5 = 4.5); the reassigned one does not",
          db.session.get(PickClaim, c3c).points == 4.5, str(db.session.get(PickClaim, c3c).points))
    # report-only window: Done without reporting is refused
    db.session.add(PickBill(bill_no="RW1", items=2))
    db.session.commit()
    rw = PickBill.query.filter_by(bill_no="RW1").first()
    t1 = datetime.utcnow() - timedelta(seconds=2 * 30 + 100)        # past normal + find, inside report
    rc = A.claim_items(ids["p1"], rw.id, 2, now=t1)
    check("9.21 The phase is 'report' after normal + find time", A.pick_phase(rc, datetime.utcnow()) == "report")
    try:
        A.finish_claim(ids["p1"], rc.id)
        locked = False
    except A.PickError as exc:
        locked = "Report" in str(exc)
    check("9.22 In report-only time, picking is locked: Done is refused", locked)
    rc2, _ = A.finish_claim(ids["p1"], rc.id, missing=[("Crocin", 2)])
    check("9.23 Reporting is still allowed then, with no points cut on picked items", rc2.outcome == "report")
    # idle warning: free picker leaves a waiting bill 2 minutes
    Notification.query.filter_by(emp_id=ids["p2"]).delete()
    PickEvent.query.filter_by(kind="idle").delete()
    db.session.get(Employee, ids["p2"]).last_seen_at = datetime.utcnow()   # p2 is on the picking screen
    for c in PickClaim.query.filter_by(picker_id=ids["p2"]).all():   # p2 has been free 5 minutes
        if c.done_at:
            c.done_at = datetime.utcnow() - timedelta(minutes=5)
    db.session.commit()

    db.session.add(PickBill(bill_no="ID1", items=3, created_at=datetime.utcnow() - timedelta(minutes=3)))
    db.session.commit()
    A.alert_late_picks()
    warn = Notification.query.filter_by(emp_id=ids["p2"]).filter(Notification.title.like("Please take%")).first()
    check("9.24 A free picker who leaves a bill waiting 2 min gets a warning", warn is not None and "deduction" in (warn.body or ""))
    check("9.25 ...and it is noted in the late report for the assigner",
          PickEvent.query.filter_by(kind="idle", picker_id=ids["p2"]).count() == 1)
    before = A.picker_pick_stats(ids["p2"], [date.today()])["points"]
    A.alert_late_picks()
    check("9.26 No points are taken for it yet, and it warns only once",
          A.picker_pick_stats(ids["p2"], [date.today()])["points"] == before
          and PickEvent.query.filter_by(kind="idle", picker_id=ids["p2"]).count() == 1)
pd = c1.get("/dashboard").data.decode()
check("9.27 The picker's scorecard order is Bills, Items, On time, Points",
      pd.index(">Bills<") < pd.index(">Items<") < pd.index(">On time<") < pd.index(">Points<"))
ad = ca.get("/dashboard").data.decode()
check("9.28 The assigner's form shows the prefix and no 'e.g.' hint", 'id="pkPrefix"' in ad and "e.g." not in ad.split('id="pkAdd"')[1][:900])

# ---- 10. Sections: leaderboard, bill-wise, timeline, reschedule, close day ----
sb = c1.get("/pick/state.json").get_json()
board = sb["board"]
check("10.1 The leaderboard ranks by bills first, then items",
      all((board[i]["bills"], board[i]["items"]) >= (board[i + 1]["bills"], board[i + 1]["items"]) for i in range(len(board) - 1)),
      str([(r["name"], r["bills"], r["items"]) for r in board]))
check("10.2 Every picker is on it with a rank", all("rank" in r for r in board) and any(r["id"] == ids["p1"] for r in board))
check("10.3 The picker sees their own bills, bill by bill", sb["my_bills"] and all("bill_no" in r and "items" in r for r in sb["my_bills"]))
tl = c1.get("/pick/bill/%d.json" % NB).get_json()
check("10.4 A bill's timeline lists every grab with who, items, times and outcome",
      tl["bill_no"].endswith("5001") and len(tl["grabs"]) >= 2 and all(g["took"] and g["picker"] for g in tl["grabs"]))
check("10.5 ...and the missing items with who reported them", any(m["item"] == "Dolo 650" for m in tl["missing"]))
check("10.6 ...and when it was added and completed, in 12-hour time", tl["added"].endswith(("AM", "PM")) and tl["completed"])
code, j = jpost(ca, "/pick/bill", {"bill_no": "6001", "items": 3})
with app.app_context():
    RB = PickBill.query.filter_by(bill_no=A.fy_prefix() + "6001").first().id
tomorrow = (datetime.utcnow() + A.IST + timedelta(days=1)).strftime("%Y-%m-%d")
code, j = jpost(c1, "/pick/bill/%d/reschedule" % RB, {"date": tomorrow, "time": "10:30"})
check("10.7 Only the assigner can reschedule", code == 403)
code, j = jpost(ca, "/pick/bill/%d/reschedule" % RB, {"date": "2020-01-01", "time": "10:00"})
check("10.8 A time in the past is refused", code == 400)
code, j = jpost(ca, "/pick/bill/%d/reschedule" % RB, {"date": tomorrow, "time": "10:30"})
s10 = ca.get("/pick/state.json").get_json()
check("10.9 Rescheduled bills leave the live list and appear under Rescheduled with the time",
      code == 200 and not any(b["id"] == RB for b in s10["bills"] if b["status"] == "open")
      and any(r["id"] == RB and r["when"] == "10:30 AM" for r in s10["scheduled"]), str(s10["scheduled"]))
code, j = jpost(c2, "/pick/claim", {"bill_id": RB, "items": 1})
check("10.10 Pickers can't take a rescheduled bill", code == 400)
with app.app_context():
    db.session.get(PickBill, RB).scheduled_for = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    A.alert_late_picks()
    check("10.11 At its time a rescheduled bill comes back on its own", db.session.get(PickBill, RB).status == "open")
    for cl in PickClaim.query.filter_by(status="active").all():
        cl.status = "cancelled"
    db.session.commit()
    A.PickDay.query.delete()
    db.session.commit()
code, j = jpost(c1, "/pick/close_day")
check("10.12 Only the assigner can close the day", code == 403)
code, j = jpost(ca, "/pick/close_day")
with app.app_context():
    pdz = A.PickDay.query.filter_by(day=date.today()).first()
    start = datetime.combine(date.today(), datetime.min.time())
    n_today = PickBill.query.filter(PickBill.created_at >= start, PickBill.status != "cancelled").count()
    check("10.13 Closing the day counts today's bills (not cancelled)", code == 200 and pdz and pdz.total_bills == n_today,
          "%s vs %s: %s" % (pdz and pdz.total_bills, n_today, j))
    check("10.14 Unfinished bills move to Rescheduled for 9:00 AM next day",
          db.session.get(PickBill, RB).status == "scheduled"
          and (db.session.get(PickBill, RB).scheduled_for + A.IST).strftime("%H:%M") == "09:00")
    row = A.checker_team_day_totals([date.today()])[date.today()]
    check("10.15 The checkers' day total is fixed to the closed day's bills", row["total"] == n_today and row["mode"] == "assigned", str(row))
code, j = jpost(ca, "/pick/close_day")
check("10.16 A day can be closed only once", code == 400)
with app.app_context():
    chk = Employee.query.filter_by(email="pk_chk@x.com").first()
    if not chk:
        chk = Employee(name="PK Checker", email="pk_chk@x.com", staff_type="checker")
        chk.set_password(PW)
        db.session.add(chk)
        db.session.commit()
cc = login("pk_chk@x.com")
cd = cc.get("/dashboard").data.decode()
check("10.17 The checker's day-total box is filled in and locked", ('value="%d" readonly' % n_today) in cd and "Pick Assigner closed today" in cd)
with app.app_context():
    A.PickDay.query.delete()
    e = Employee.query.filter_by(email="pk_chk@x.com").first()
    if e:
        db.session.delete(e)
    db.session.commit()
ad = ca.get("/dashboard").data.decode()
check("10.18 The assigner's screen has Live, Late report, Leaderboard, Rescheduled and Rules tabs",
      all('data-tab="%s"' % t in ad for t in ("live", "late", "board", "sched", "rules")))
check("10.19 The picker's screen has Live, Leaderboard, My bills and Rules tabs",
      all('data-tab="%s"' % t in c1.get("/dashboard").data.decode() for t in ("live", "board", "mine", "rules")))

# ---- 11. Admin: picking page, reports, CSV; 12-hour time; Enter key ------------
with app.app_context():
    adm = Employee.query.filter_by(email="pk_adm@x.com").first()
    if not adm:
        adm = Employee(name="PK Admin", email="pk_adm@x.com", staff_type="picker", is_admin=True)
        adm.set_password(PW)
        db.session.add(adm)
        db.session.commit()
cadm = login("pk_adm@x.com")
pa = cadm.get("/picking").data.decode()
check("11.1 The admin has a Picking page with the live floor", 'id="pickApp"' in pa and 'data-role="assigner"' in pa)
check("11.2 ...listing every bill number of the day", 'id="paBills"' in pa and (A.fy_prefix() + "5001") in pa)
check("11.3 ...and every missing item with who reported it", 'id="paMissing"' in pa and "Dolo 650" in pa and "PK P3" in pa)
check("11.3b No dict-method text leaks onto the page (the .items Jinja trap)", "built-in method" not in pa)
check("11.4 The admin page links to it", 'href="/picking"' in cadm.get("/admin_dashboard").data.decode())
st_admin = cadm.get("/pick/state.json").get_json()
check("11.5 The admin sees the assigner's view of the live data", "pickers" in st_admin and "events" in st_admin)
csvr = cadm.get("/picking/report.csv")
body = csvr.data.decode()
check("11.6 The day's report downloads as CSV with bills and missing items",
      csvr.mimetype == "text/csv" and "Bill number" in body and (A.fy_prefix() + "5001") in body and "Dolo 650" in body)
check("11.7 Staff cannot open the admin picking page", c1.get("/picking", follow_redirects=True).data.find(b'id="paBills"') == -1)
old = cadm.get("/picking?date=2020-01-01").data.decode()
check("11.8 Another day's report can be shown", "No bills on this day." in old)
with app.app_context():
    check("11.9 Times show as 12-hour Indian time", A._t12_filter(datetime(2026, 11, 9, 8, 35)) == "2:05 PM"
          and A._dt12_filter(datetime(2026, 11, 9, 20, 0)) == "10 Nov 1:30 AM")
    e = Employee.query.filter_by(email="pk_adm@x.com").first()
    if e:
        db.session.delete(e)
        db.session.commit()
import glob as _glob  # noqa: E402
_no24 = [f for f in _glob.glob(os.path.join(REPO, "templates", "*.html"))
         if "strftime('%H:%M')" in open(f).read() or "strftime('%d %b %H:%M')" in open(f).read()]
check("11.10 No page shows 24-hour times any more", not _no24, str(_no24))
_forms = [f for f in _glob.glob(os.path.join(REPO, "templates", "*.html"))
          if "<form" in open(f).read() and not os.path.basename(f).startswith("_") and "js/enter-next.js" not in open(f).read()]
check("11.11 Every page with a form has Enter-to-next-field", not _forms, str(_forms))

# ---- 12. Staff profile for pickers, bill flowchart, per-picker countdown -------
with app.app_context():
    adm = Employee.query.filter_by(email="pk_adm2@x.com").first()
    if not adm:
        adm = Employee(name="PK Admin2", email="pk_adm2@x.com", staff_type="picker", is_admin=True)
        adm.set_password(PW)
        db.session.add(adm)
        db.session.commit()
ca2 = login("pk_adm2@x.com")
prof = ca2.get("/staff/%d" % ids["p3"]).data.decode()
check("12.1 A picker's profile has the bill-by-bill picking section", 'id="picking"' in prof and "Picking — bill by bill" in prof)
check("12.2 ...with Today, This week and This month in the order Bills, Items, On time, Points",
      all(x in prof for x in ("Today", "This week", "This month")) and prof.index("<span>Bills</span>") < prof.index("<span>Items</span>")
      < prof.index("<span>On time</span>") < prof.index("<span>Points</span>"))
check("12.3 ...each grab with took and done times and a flowchart button", 'id="ppGrabs"' in prof and 'data-bill-flow="%d"' % NB in prof)
check("12.4 ...and a Bill-wise view", 'id="ppBills"' in prof and 'data-ppview="bills"' in prof)
check("12.5 ...and the day's late report", "Late report" in prof and "Dolo 650" in prof)
check("12.6 The old picker figures (bill fulfilment, workspace, VWCR) are gone from a picker's profile",
      "Bill Fulfilment" not in prof and "Sales Bills Picked" not in prof)
check("12.7 A picker can open their own profile; another picker's is refused",
      'id="picking"' in c3.get("/staff/%d" % ids["p3"]).data.decode()
      and 'id="picking"' not in c1.get("/staff/%d" % ids["p3"], follow_redirects=True).data.decode())
tl2 = ca2.get("/pick/bill/%d.json" % NB).get_json()
check("12.8 The flowchart gets exact times for every grab (start, normal, extra, report, done)",
      all(all(g.get(k) for k in ("s", "n", "f", "r")) for g in tl2["grabs"]) and tl2["t_added"])
check("12.9 The flowchart script is on the staff, assigner, admin and profile pages",
      all("js/billflow.js" in open(os.path.join(REPO, "templates", f)).read()
          for f in ("dashboard.html", "picking_admin.html", "staff_detail.html")))
code, j = jpost(ca, "/pick/bill", {"bill_no": "7001", "items": 4})
with app.app_context():
    CB = PickBill.query.filter_by(bill_no=A.fy_prefix() + "7001").first().id
    for cl in PickClaim.query.filter_by(status="active").all():
        cl.status = "cancelled"
    db.session.commit()
jpost(c1, "/pick/claim", {"bill_id": CB, "items": 2})
row = next(b for b in ca.get("/pick/state.json").get_json()["bills"] if b["id"] == CB)
t = next(t for t in row["taken"] if t["name"] == "PK P1")
check("12.10 Under a live bill, each picker shows their own time left", t["left"] is not None and 0 < t["left"] <= 60, str(t))
with app.app_context():
    e = Employee.query.filter_by(email="pk_adm2@x.com").first()
    if e:
        db.session.delete(e)
        db.session.commit()

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
check("8.5 Every page shows the trial label", b">Trial copy<" in tc.get("/login").data)
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
    A.PickMissing.query.delete()
    PickEvent.query.delete()
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
