"""Staff today: the admin's report of everyone who came in on a day.

The owner asked for the report of each person who signed in today,
specially pickers: all their work with every time, on a page of its own and
as a PDF. Sign-ins and sign-outs are now recorded (StaffLogin). Pickers are
shown grab by grab: bill, items taken, picked, not found (with item names),
took at, normal time ends, done at, time given, time taken, result (normal /
extra / report / late N min / picking now) and points.

Run:  python3.13 tests_staff_today.py
"""
import os
import re
import sys
from datetime import date, datetime, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import (app, db, Employee, KPIEntry, PickBill, PickClaim, PickMissing, PickEvent,  # noqa: E402
                 StaffLogin)
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def mk(email, st, name, admin=False):
    e = Employee.query.filter_by(email=email).first()
    if not e:
        e = Employee(name=name, email=email, staff_type=st, is_admin=admin)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    e.staff_type, e.name, e.is_admin, e.last_seen_at = st, name, admin, None
    for M, col in ((PickClaim, PickClaim.picker_id), (KPIEntry, KPIEntry.emp_id), (StaffLogin, StaffLogin.emp_id),
                   (PickMissing, PickMissing.picker_id), (PickEvent, PickEvent.picker_id), (A.MultitaskEntry, A.MultitaskEntry.emp_id)):
        M.query.filter(col == e.id).delete(synchronize_session=False)
    db.session.commit()
    return e


def login(email):
    c = csrf_client(app.test_client())
    c.post("/login", data={"email": email, "password": PW}, follow_redirects=True)
    return c


with app.app_context():
    db.create_all()
    PickMissing.query.delete()
    PickClaim.query.delete()
    PickBill.query.delete()
    StaffLogin.query.delete()
    db.session.commit()
    adm = mk("st_adm@x.com", "picker", "ST Admin", admin=True)
    ria = mk("st_ria@x.com", "picker", "ST Ria")
    om = mk("st_om@x.com", "picker", "ST Om")
    asg = mk("st_asg@x.com", "pick_assigner", "ST Asg")
    chk = mk("st_chk@x.com", "checker", "ST Chk")
    idle = mk("st_idle@x.com", "packer", "ST Idle")
    gone = mk("st_gone@x.com", "picker", "ST Gone")
    RIA, OM, ASG, CHK, IDLE, GONE, ADM = ria.id, om.id, asg.id, chk.id, idle.id, gone.id, adm.id

# ---- 1. Sign-ins are recorded ------------------------------------------------
c_ria = login("st_ria@x.com")
c_idle = login("st_idle@x.com")
c_adm = login("st_adm@x.com")
with app.app_context():
    rows = StaffLogin.query.filter_by(emp_id=RIA).all()
    check("1.1 A staff sign-in writes a row for today", len(rows) == 1 and rows[0].kind == "login" and rows[0].day == date.today())
    check("1.2 Admin sign-ins are not tracked", StaffLogin.query.filter_by(emp_id=ADM).count() == 0)
c_idle.get("/logout")
with app.app_context():
    check("1.3 Log out writes a sign-out row",
          [r.kind for r in StaffLogin.query.filter_by(emp_id=IDLE).order_by(StaffLogin.id).all()] == ["login", "logout"])
    check("1.4 Sign-ins table is on the Diagnostics list", any(t == "staff_logins" for _, t in
          [(x["label"], x["table"]) for x in A.database_check()]))

# ---- 2. Picking grabs with every time -----------------------------------------
with app.app_context():
    n_sec, u_sec = A.pick_times()
    half = A.PICK_EXTRA_SECONDS / 2.0
    b1 = A.add_pick_bill("7101", 5, ASG)
    B1 = b1.id
    t0 = datetime.utcnow() - timedelta(minutes=40)
    # Ria: 3 items, done in 60 s (normal time)
    c1 = A.claim_items(RIA, B1, 3, now=t0)
    A.finish_claim(RIA, c1.id, now=t0 + timedelta(seconds=60))
    # Ria: 2 items, done 90 s after report time ended (late 2 min)
    t1 = t0 + timedelta(minutes=5)
    c2 = A.claim_items(RIA, B1, 2, now=t1)
    late_done = c2.due_at + timedelta(seconds=2 * half + 90)
    A.finish_claim(RIA, c2.id, now=late_done)
    B2 = A.add_pick_bill("7102U", 4, ASG).id
    # Om: urgent bill, 3 items, one not found (Crocin)
    t2 = t0 + timedelta(minutes=2)
    c3 = A.claim_items(OM, B2, 3, now=t2)
    A.finish_claim(OM, c3.id, now=t2 + timedelta(seconds=50), picked=2, missing=[("Crocin", 1)])
    # Om takes the last item and is still picking
    c4 = A.claim_items(OM, B2, 1, now=datetime.utcnow())
    # Checker typed an entry today but never signed in on this server
    db.session.add(KPIEntry(emp_id=CHK, entry_date=date.today(), sales_bills_open=40, cs_sales_open=5, checked=300))
    # A picker who signed in yesterday only: not in today's report
    db.session.add(StaffLogin(emp_id=GONE, at=datetime.utcnow() - timedelta(days=1), day=date.today() - timedelta(days=1)))
    db.session.commit()

    r = A.staff_day_report(date.today())
    by = {p["id"]: p for p in r["people"]}
    check("2.1 Everyone who signed in or worked today is listed",
          {RIA, OM, ASG, CHK, IDLE} <= set(by), str(sorted(by)))
    check("2.2 Someone who only came yesterday is not listed", GONE not in by)
    check("2.3 Admins are never listed", ADM not in by)
    check("2.4 Pickers come first", r["people"][0]["staff_type"] == "picker")
    g = by[RIA]["picks"]
    check("2.5 Ria has two grabs, in time order", [x["items"] for x in g] == [3, 2])
    check("2.6 Normal-time grab: result, points", g[0]["result"] == "Normal time" and g[0]["points"] == 3 * A.PICK_ONTIME_RATE, str(g[0]))
    check("2.7 Time given = items x item time", g[0]["allowed"] == A._mmss(3 * n_sec), g[0]["allowed"])
    check("2.8 Time taken = take to Done", g[0]["taken"] == "1:00", g[0]["taken"])
    check("2.9 Took at / normal ends / done at shown with seconds",
          g[0]["took_at"] == A.ist_sec(t0) and g[0]["done_at"] == A.ist_sec(t0 + timedelta(seconds=60))
          and g[0]["due_at"].count(":") == 2)
    check("2.10 Late grab says how late", g[1]["result"] == "Late 1:30" and g[1]["outcome"] == "late", g[1]["result"])
    check("2.11 Late grab points are negative", g[1]["points"] == -2.0, str(g[1]["points"]))
    k = by[RIA]["pick"]
    check("2.12 Ria totals: bills, items, points, late",
          k["bills"] == 1 and k["items"] == 5 and k["late"] == 1 and k["ontime"] == 1
          and k["points"] == round(3 * A.PICK_ONTIME_RATE - 2, 1), str(k))
    check("2.13 Picking time and free time are worked out", k["picking_time"] and k["free_time"], str(k))
    check("2.14 Items per hour is worked out", k["rate"] and k["rate"] > 0)
    og = by[OM]["picks"]
    check("2.15 Not found shows the item name", og[0]["missing"] == 1 and "Crocin" in og[0]["missing_names"], str(og[0]))
    check("2.16 Urgent bill is marked", og[0]["urgent"] is True)
    check("2.17 A running grab shows 'Picking now'", og[1]["result"] == "Picking now" and og[1]["points"] is None)
    check("2.18 No 'bills added' count for the assigner", "bills_added" not in by[ASG])
    check("2.19 No typed figures for other staff", all("entry" not in p for p in r["people"]))
    check("2.20 Sign-in time shown", by[RIA]["first_in"] != "" and by[CHK]["first_in"] == "")
    check("2.21 Sign-out time shown", by[IDLE]["last_out"] != "")
    check("2.22 Day totals", r["totals"]["items"] == 7 and r["totals"]["late"] == 1 and r["totals"]["missing"] == 1, str(r["totals"]))
    one = A.staff_day_report(date.today(), RIA)
    check("2.23 One person only", [p["id"] for p in one["people"]] == [RIA] and one["emp_name"] == "ST Ria")
    check("2.24 Another day leaves out today's people",
          not ({RIA, OM, ASG, IDLE} & {p["id"] for p in A.staff_day_report(date.today() - timedelta(days=3))["people"]}))

# ---- 3. Fixed number of database questions ------------------------------------
with app.app_context():
    from sqlalchemy import event
    n = {"q": 0}

    def _count(*a, **k):
        n["q"] += 1
    event.listen(db.engine, "before_cursor_execute", _count)
    A.staff_day_report(date.today())
    q1 = n["q"]
    for i in range(4):
        e = mk("st_x%d@x.com" % i, "picker", "ST X%d" % i)
        b = A.add_pick_bill(str(7200 + i), 2, ASG)
        cl = A.claim_items(e.id, b.id, 2, now=datetime.utcnow() - timedelta(minutes=3))
        A.finish_claim(e.id, cl.id, now=datetime.utcnow() - timedelta(minutes=2))
    n["q"] = 0
    A.staff_day_report(date.today())
    q2 = n["q"]
    event.remove(db.engine, "before_cursor_execute", _count)
    check("3.1 More pickers and bills do not add database questions", q2 <= q1, "%d -> %d" % (q1, q2))

# ---- 4. The page -------------------------------------------------------------
html = c_adm.get("/admin/staff-today").get_data(as_text=True)
check("4.1 Admin page opens", 'id="staffToday"' in html)
check("4.2 Shows names, results, item names",
      "ST Ria" in html and "Late 1:30" in html and "Normal time" in html and "Crocin" in html and "Picking now" in html)
check("4.3 Shows every time column", all(h in html for h in ("Took at", "Normal time ends", "Done at", "Time given", "Time taken")))
check("4.4 Phone layout: each cell carries its label", 'data-l="Took at"' in html and "@media (max-width:760px)" in html)
check("4.5 Download PDF link", "/admin/staff-today.pdf?date=" in html)
check("4.6 Per-person PDF link", "emp=%d" % RIA in html)
check("4.6b No typed figures or 'bills added' on the page",
      "bills added" not in html and "Bills checked" not in html and "Day entry" not in html)
check("4.6c Only people who picked get a detail card", 'id="p%d"' % RIA in html and 'id="p%d"' % CHK not in html
      and 'id="p%d"' % ASG not in html)
one = c_adm.get("/admin/staff-today?emp=%d" % OM).get_data(as_text=True)
check("4.7 Person filter", 'id="p%d"' % OM in one and 'id="p%d"' % RIA not in one)
fut = c_adm.get("/admin/staff-today?date=2999-01-01").get_data(as_text=True)
check("4.8 A future date falls back to today", date.today().isoformat() in fut)
check("4.9 Staff cannot open it", c_ria.get("/admin/staff-today").status_code in (302, 403))
check("4.10 Staff cannot download it", c_ria.get("/admin/staff-today.pdf").status_code in (302, 403))
adm_html = c_adm.get("/admin_dashboard").get_data(as_text=True)
check("4.11 Admin dashboard links to it (big button and Go to)",
      'href="/admin/staff-today" id="qaStaffToday"' in adm_html and 'id="navStaffToday"' in adm_html)

# ---- 5. PDF ------------------------------------------------------------------
r = c_adm.get("/admin/staff-today.pdf")
check("5.1 PDF downloads", r.status_code == 200 and r.data[:4] == b"%PDF" and len(r.data) > 2000)
check("5.2 PDF file name has the date", date.today().isoformat() in r.headers.get("Content-Disposition", ""))
r = c_adm.get("/admin/staff-today.pdf?emp=%d" % RIA)
check("5.3 One person's PDF", r.status_code == 200 and r.data[:4] == b"%PDF" and "st-ria" in r.headers.get("Content-Disposition", ""))
r = c_adm.get("/admin/staff-today.pdf?date=%s" % (date.today() - timedelta(days=5)).isoformat())
check("5.4 Empty day still makes a PDF", r.status_code == 200 and r.data[:4] == b"%PDF")
import picking_pdf  # noqa: E402
with app.app_context():
    txt = picking_pdf._rules({"times": A.pick_times(), "extra_seconds": A.PICK_EXTRA_SECONDS})
    check("5.5 PDF footers state today's item time", ("%d s normal" % A.pick_times()[0]) in txt and "62.5 s" in txt, txt)

# ---- 6. Clean-up on delete ---------------------------------------------------
with app.app_context():
    v = mk("st_del@x.com", "picker", "ST Del")
    db.session.add(StaffLogin(emp_id=v.id, at=datetime.utcnow(), day=date.today()))
    db.session.commit()
    VID = v.id
c_adm.post("/admin/delete_user/%d" % VID, follow_redirects=True)
with app.app_context():
    check("6.1 Deleting a staff member deletes their sign-ins",
          db.session.get(Employee, VID) is None and StaffLogin.query.filter_by(emp_id=VID).count() == 0)

# ---- 7. Pick Assigner: today's totals, closed total for checkers ---------------
with app.app_context():
    for M in (PickMissing, PickClaim):
        M.query.delete()
    PickBill.query.delete()
    A.PickDay.query.filter_by(day=date.today()).delete()
    db.session.commit()
    a1 = A.add_pick_bill("8101", 4, ASG).id
    A.add_pick_bill("8102", 3, ASG)
    A.add_pick_bill("8103", 2, ASG)
    cc = A.claim_items(RIA, a1, 4, now=datetime.utcnow() - timedelta(minutes=2))
    A.finish_claim(RIA, cc.id, now=datetime.utcnow() - timedelta(minutes=1))
c_asg = login("st_asg@x.com")
st = c_asg.get("/pick/state.json").get_json()
d = st.get("day") or {}
check("7.1 Assigner sees bills added today", d.get("added") == 3, str(d))
check("7.2 ...completed today", d.get("done") == 1, str(d))
check("7.3 ...pending now and items still to pick", d.get("pending") == 2 and d.get("items_left") == 5, str(d))
check("7.4 Not closed yet", st.get("day_closed") is None)
page = c_asg.get("/dashboard").get_data(as_text=True)
check("7.5 The assigner screen has the Today strip", 'id="pkToday"' in page and "Closed total (checkers)" in page)
with app.app_context():
    A.close_pick_day(ASG)
    st = c_asg.get("/pick/state.json").get_json()
    check("7.6 After Close today the closed total is the day's bills", st.get("day_closed") == 3)
    check("7.7 ...and it is the checkers' fixed day total",
          A.checker_team_day_totals([date.today()])[date.today()]["total"] == 3,
          str(A.checker_team_day_totals([date.today()])[date.today()]))
    check("7.8 Open bills moved to Rescheduled when the day closed",
          PickBill.query.filter_by(status="open").count() == 0)
sp = c_ria.get("/pick/state.json").get_json()
check("7.9 A picker sees no bill details",
      all(b["taken"] == [] and b["missing_rows"] == [] for b in sp.get("bills", [])))

# ---- 8. Pause time is not shown anywhere ------------------------------------
_pk = open(os.path.join(REPO, "templates", "_picking.html")).read()
check("8.1 A paused bill shows no 'paused for' counter", "paused for" not in _pk and "Paused · timers stopped" in _pk)
check("8.2 A paused picker's chip shows no time left", '(r.paused ? "" : \' · <b class="pk-left"></b>\')' in _pk)
check("8.3 A paused picker's own clock shows no time", '$("pkClock").textContent = stopped ? "" :' in _pk)
with app.app_context():
    b = A.add_pick_bill("8301", 2, ASG)
    cl = A.claim_items(OM, b.id, 2, now=datetime.utcnow() - timedelta(minutes=3))
    A.pause_bill(b.id, now=datetime.utcnow() - timedelta(minutes=2, seconds=50))
    A.resume_bill(b.id, now=datetime.utcnow() - timedelta(minutes=1))
    A.finish_claim(OM, cl.id, now=datetime.utcnow())
    g = [x for x in A.staff_day_report(date.today(), OM)["people"][0]["picks"] if x["bill_no"].endswith("8301")][0]
    check("8.4 Time given is items x item time, paused time never added", g["allowed"] == A._mmss(2 * A.pick_times()[0]), g["allowed"])

# ---- 9. Bill search: any date, details for assigner and admin only ------------
with app.app_context():
    old = A.add_pick_bill("945612", 3, ASG)
    old.created_at = datetime.utcnow() - timedelta(days=20)
    db.session.commit()
    OLD = old.id
    cs = A.claim_items(RIA, OLD, 2, now=old.created_at + timedelta(minutes=1))
    A.finish_claim(RIA, cs.id, now=old.created_at + timedelta(minutes=2))
j = c_adm.get("/pick/bills/find.json?q=5612").get_json()
hit = [b for b in j.get("bills", []) if b["id"] == OLD]
check("9.1 Admin finds a bill from 20 days ago by part of its number", bool(hit), str(j)[:200])
check("9.2 Result has date, items and how much is left", hit and hit[0]["items"] == 3 and hit[0]["left"] == 1 and hit[0]["date"])
c_asg2 = login("st_asg@x.com")
check("9.3 The Pick Assigner can search too", any(b["id"] == OLD for b in c_asg2.get("/pick/bills/find.json?q=945612").get_json()["bills"]))
c_ria2 = login("st_ria@x.com")
check("9.4 A picker's search gives a summary only, never the full detail",
      c_ria2.get("/pick/bills/find.json?q=945612").get_json()["detail"] is False
      and c_ria2.get("/pick/bill/%d.json" % OLD).status_code == 403)
check("9.5 Letters only finds nothing", c_adm.get("/pick/bills/find.json?q=abc").get_json()["bills"] == [])
tl = c_adm.get("/pick/bill/%d.json" % OLD).get_json()
check("9.6 Tapping it opens everything: staff, items, times", tl["grabs"] and tl["grabs"][0]["picker"] == "ST Ria"
      and tl["grabs"][0]["items"] == 2 and tl["grabs"][0]["took"] and tl["grabs"][0]["done"])
adm_html = c_adm.get("/admin_dashboard").get_data(as_text=True)
check("9.7 Admin dashboard has a 'Find a bill' box", 'data-bill-search="qaFindOut"' in adm_html and "js/billflow.js" in adm_html)
check("9.8 The assigner's search also looks at every date", 'data-bill-search="pkFindAll"' in c_asg2.get("/dashboard").get_data(as_text=True))
_bf = open(os.path.join(REPO, "static", "js", "billflow.js")).read()
check("9.9 Search results open the bill's full detail", "/pick/bills/find.json" in _bf and 'data-bill-flow="\' + b.id' in _bf)
check("9.10 Free pickers get 5 minutes after a bill is added before waiting counts", A.PICK_IDLE_SECONDS == 300)

# ---- 10. Missing items go to the purchaser ----------------------------------
with app.app_context():
    pur = mk("st_pur@x.com", "purchaser", "ST Pur")
    PUR = pur.id
    A.Notification.query.filter_by(emp_id=PUR).delete()
    db.session.commit()
    mb = A.add_pick_bill("8501", 3, ASG)
    mc = A.claim_items(RIA, mb.id, 3, now=datetime.utcnow() - timedelta(seconds=40))
    A.finish_claim(RIA, mc.id, picked=2, missing=[("Volini gel", 1)])
    MB = mb.id
    check("10.1 Picker's part ends at Done; the bill waits for the purchaser",
          db.session.get(PickBill, MB).status == "open" and A.pick_bill_progress(db.session.get(PickBill, MB))["missing"] == 1)
    n = A.Notification.query.filter_by(emp_id=PUR).filter(A.Notification.title.like("Missing items%")).first()
    check("10.2 The purchaser is alerted, with the item named", n is not None and "Volini gel" in (n.body or ""))
c_pur = login("st_pur@x.com")
dash = c_pur.get("/dashboard").get_data(as_text=True)
check("10.3 The purchaser's dashboard has the 'Missing items to sort' box", 'id="purMissing"' in dash)
check("10.4 Staff who are not purchasers do not get the box", 'id="purMissing"' not in c_ria.get("/dashboard").get_data(as_text=True))
adm = c_adm.get("/admin_dashboard").get_data(as_text=True)
check("10.5 The admin sees the box on the Today tab", re.search(r'id="purMissing" data-pane="today"', adm) is not None)
asg_page = c_asg2.get("/dashboard").get_data(as_text=True)
check("10.6 The assigner has no Reassign / Not available buttons, just 'with purchaser'",
      'data-act="reassign"' not in asg_page and "with purchaser" in asg_page)
mid = [m for m in c_pur.get("/pick/missing/open.json").get_json()["items"] if m["bill_id"] == MB][0]["id"]
r = c_pur.post("/pick/missing/%d/done" % mid, json={}, headers={"X-Requested-With": "fetch"})
with app.app_context():
    check("10.7 Purchaser taps Done: item sorted, bill complete, picker paid",
          r.status_code == 200 and db.session.get(PickBill, MB).status == "done"
          and db.session.get(PickClaim, mc.id).points == round(3 * A.PICK_ONTIME_RATE, 1), str(db.session.get(PickClaim, mc.id).points))
with app.app_context():
    check("10.8 Purchasers are on the alert list",
          PUR in [e.id for e in A._purchasers()])

# ---- 11. An urgent bill never cuts a pick already started ---------------------
with app.app_context():
    PickClaim.query.filter_by(status="active").update({"status": "cancelled"})
    PickBill.query.filter_by(status="open").update({"status": "cancelled"})
    db.session.commit()
    nb = A.add_pick_bill("8601", 6, ASG).id
    a1 = A.claim_items(OM, nb, 2)                       # Om is picking a normal bill
    seen_before = int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds() * 1000) - 2000
    ub = A.add_pick_bill("8602U", 2, ASG)
    UB = ub.id
    check("11.1 Adding an urgent bill leaves a running normal pick alone",
          db.session.get(PickClaim, a1.id).status == "active")
    # Ria tapped Pick on the normal bill from a screen that had not shown the urgent bill yet
    r1 = A.claim_items(RIA, nb, 2, seen_ver=seen_before)
    check("11.2 A pick tapped before the urgent bill reached the screen goes ahead (not cut)", r1.status == "active")
    A.finish_claim(RIA, r1.id)
    try:
        A.claim_items(RIA, nb, 1, seen_ver=int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds() * 1000) + 1000)
        ok_ = False
    except A.PickError as e:
        ok_ = "Urgent" in str(e)
    check("11.3 Once the picker has seen the urgent bill, it still comes first", ok_)
    ub.created_at = datetime.utcnow() - timedelta(seconds=A.PICK_URGENT_UNSEEN + 5)
    db.session.commit()
    try:
        A.claim_items(RIA, nb, 1, seen_ver=seen_before - 600000)
        ok_ = False
    except A.PickError:
        ok_ = True
    check("11.4 A very old screen cannot skip an urgent bill (only 60 s of grace)", ok_)
    u1 = A.claim_items(RIA, UB, 2)                      # someone picks the urgent bill
    check("11.5 Someone picking the urgent bill does not cut other pickers' normal picks",
          db.session.get(PickClaim, a1.id).status == "active" and u1.status == "active")
    A.finish_claim(RIA, u1.id)
    A.finish_claim(OM, a1.id)
    check("11.6 The normal pick finishes with its points", db.session.get(PickClaim, a1.id).status == "done"
          and db.session.get(PickClaim, a1.id).points > 0)
_pkt = open(os.path.join(REPO, "templates", "_picking.html")).read()
check("11.7 The phone sends the screen version with Pick, and ignores refreshes older than the last tap",
      "seen_ver: (lastState && lastState.ver)" in _pkt and "asked < lastAct" in _pkt)

# ---- 12. Pick Assigner: up to 3 phones at once --------------------------------
phones = [login("st_asg@x.com") for _ in range(4)]
codes = [ph.get("/pick/state.json").status_code for ph in phones]
check("12.1 Three assigner phones stay signed in; the 4th sign-in signs out the oldest",
      codes == [401, 200, 200, 200], str(codes))
check("12.2 The signed-out phone is told why", "other phones" in (phones[0].get("/pick/state.json").get_json() or {}).get("message", "")
      or phones[0].get("/pick/state.json").status_code in (302, 401))
p1, p2 = login("st_om@x.com"), login("st_om@x.com")
check("12.3 Other staff still: one phone at a time", p1.get("/pick/state.json").status_code == 401
      and p2.get("/pick/state.json").status_code == 200)

# ---- 13. Dates on bills, search for pickers ------------------------------------
st = phones[3].get("/pick/state.json").get_json()
bl = [b for b in st["bills"] if b["bill_no"].endswith("8601")]
check("13.1 Bills show their date with the time", bl and re.match(r"^\d{2} \w{3}, \d{1,2}:\d{2} [AP]M$", bl[0]["added"]), bl and bl[0]["added"])
with app.app_context():
    tl = A.bill_timeline(bl[0]["id"])
    check("13.2 A bill's detail shows the date too", re.match(r"^\d{2} \w{3}, ", tl["added"]) is not None, tl["added"])
pp = p2.get("/dashboard").get_data(as_text=True)
check("13.3 Pickers have a search box for open bills", 'id="pkFindP"' in pp)
check("13.4 ...that also finds bills of any date", 'data-bill-search="pkFindPAll"' in pp)

# ---- 14. Find a bill for picker, checker and purchaser (summary only) ---------
with app.app_context():
    chk = mk("st_chk2@x.com", "checker", "ST Chk2")
c_chk = login("st_chk2@x.com")
c_pur2 = login("st_pur@x.com")
c_om = login("st_om@x.com")
for who, cl in (("picker", c_om), ("checker", c_chk), ("purchaser", c_pur2)):
    j = cl.get("/pick/bills/find.json?q=945612").get_json() or {}
    check("14.%s %s can search bills of any date" % (who[0], who), j.get("ok") and any(b["bill_no"].endswith("945612") for b in j["bills"])
          and j.get("detail") is False, str(j)[:160])
j = c_om.get("/pick/bills/find.json?q=945612").get_json()
check("14.1 Staff results carry no names (who picked stays with assigner and admin)",
      all("by" not in b and "picker" not in b for b in j["bills"]))
check("14.2 Staff still cannot open a bill's full detail", c_chk.get("/pick/bill/%d.json" % OLD).status_code == 403)
check("14.3 The assigner and admin get results they can tap", c_adm.get("/pick/bills/find.json?q=945612").get_json()["detail"] is True)
for who, cl in (("checker", c_chk), ("purchaser", c_pur2)):
    check("14.4 %s dashboard has the Find a bill bar" % who, 'id="billFind"' in cl.get("/dashboard").get_data(as_text=True))
_bf2 = open(os.path.join(REPO, "static", "js", "billflow.js")).read()
check("14.5 Staff results are plain rows, not tappable", "bf-hit--ro" in _bf2 and "d.detail ?" in _bf2)
check("14.6 /pick/me loads the bill search too", "js/billflow.js" in open(os.path.join(REPO, "templates", "pick_me.html")).read())

# ---- 15. Item quantity: any number except 0 -----------------------------------
with app.app_context():
    PickClaim.query.filter_by(status="active").update({"status": "cancelled"})
    PickBill.query.filter_by(status="open").update({"status": "cancelled"})
    db.session.commit()
    try:
        A.add_pick_bill("8701", 0, ASG); ok_ = False
    except A.PickError as e:
        ok_ = "except 0" in str(e)
    check("15.1 Item quantity 0 is refused, with the reason", ok_)
    big = A.add_pick_bill("8702", 1250, ASG)
    check("15.2 A quantity above 500 is accepted now", big.items == 1250)
    BIG = big.id
_pk2 = open(os.path.join(REPO, "templates", "_picking.html")).read()
check("15.3 The box is called Item quantity and has no upper limit",
      "Item quantity (any number except 0)" in _pk2 and 'max="500"' not in _pk2 and 'placeholder="Quantity"' in _pk2)

# ---- 16. The bill just added, and who added it, shows for a few seconds ---------
c_a3 = login("st_asg@x.com")
r = c_a3.post("/pick/bill", json={"bill_no": "8703", "items": 7}, headers={"X-Requested-With": "fetch"}).get_json()
la = (r.get("state") or {}).get("last_added") or {}
check("16.1 After Add, the reply names the bill, its quantity and who added it",
      la.get("bill_no", "").endswith("8703") and la.get("items") == 7 and la.get("by") == "ST Asg", str(la))
la2 = c_adm.get("/pick/state.json").get_json().get("last_added") or {}
check("16.2 Other assigner / admin screens see it too", la2.get("bill_no", "").endswith("8703"))
with app.app_context():
    b3 = PickBill.query.filter(PickBill.bill_no.like("%8703")).first()
    b3.created_at = datetime.utcnow() - timedelta(seconds=A.PICK_JUST_ADDED + 3)
    db.session.commit()
check("16.3 It goes away after about 10 seconds",
      not ((c_adm.get("/pick/state.json").get_json().get("last_added") or {}).get("bill_no", "")).endswith("8703"))
check("16.4 The screen has the line under the entry box", 'id="pkAdded"' in _pk2 and "function showAdded" in _pk2)

# ---- 17. A picker can delete a wrong report and redo it -----------------------
with app.app_context():
    w = A.add_pick_bill("8704", 4, ASG)
    WB = w.id
    wc = A.claim_items(RIA, WB, 4)
    A.finish_claim(RIA, wc.id, picked=3, missing=[("Wrong name", 1)])
    WM = PickMissing.query.filter_by(claim_id=wc.id).first().id
c_r3, c_o3 = login("st_ria@x.com"), login("st_om@x.com")
mm = c_r3.get("/pick/state.json").get_json().get("my_missing") or []
check("17.1 The picker sees their own report waiting with the purchaser", any(m["id"] == WM and m["item"] == "Wrong name" for m in mm), str(mm))
check("17.2 Another picker cannot delete it",
      c_o3.post("/pick/missing/%d/delete" % WM, json={}, headers={"X-Requested-With": "fetch"}).status_code == 403)
r = c_r3.post("/pick/missing/%d/delete" % WM, json={}, headers={"X-Requested-With": "fetch"})
with app.app_context():
    pr = A.pick_bill_progress(db.session.get(PickBill, WB))
    check("17.3 Delete puts the item back on the bill to take and report again",
          r.status_code == 200 and pr["left"] == 1 and pr["missing"] == 0, str(pr))
    check("17.4 The purchaser's list no longer shows it", all(m["id"] != WM for m in A.open_missing_items()))
    rc = A.claim_items(RIA, WB, 1)
    A.finish_claim(RIA, rc.id, picked=0, missing=[("Right name", 1)])
    RM = PickMissing.query.filter_by(claim_id=rc.id).first().id
c_p3 = login("st_pur@x.com")
c_p3.post("/pick/missing/%d/done" % RM, json={}, headers={"X-Requested-With": "fetch"})
check("17.5 Once the purchaser has finished it, it cannot be deleted",
      c_r3.post("/pick/missing/%d/delete" % RM, json={}, headers={"X-Requested-With": "fetch"}).status_code == 400)
check("17.6 The report form has a delete button on every row", 'data-rep-del="1"' in _pk2 and "data-miss-del" in _pk2)

# ---- 18. Admin: the missing-items report says who finished each item ------------
with app.app_context():
    rep_ = A.pick_report(date.today())
    row = [m for m in rep_["missing"] if m["item"] == "Right name"][0]
    check("18.1 Report: who reported, what happened, who finished and when",
          row["by"] == "ST Ria" and row["status"] == "Done by purchaser" and row["finished_by"] == "ST Pur" and row["finished_at"], str(row))
    row2 = [m for m in rep_["missing"] if m["item"] == "Wrong name"][0]
    check("18.2 A report the picker deleted says so", "Deleted by picker" in row2["status"], row2["status"])
pg = c_adm.get("/picking?tab=reports").get_data(as_text=True)
check("18.3 Picking > Reports shows the Finished by column", "Finished by" in pg and "ST Pur" in pg)
check("18.4 The PDF and CSV carry it too", c_adm.get("/picking/report.pdf").data[:4] == b"%PDF"
      and "Finished by" in c_adm.get("/picking/report.csv").get_data(as_text=True))
adm2 = c_adm.get("/admin_dashboard").get_data(as_text=True)
check("18.5 Admin dashboard: a Missing items report button, and Day view",
      re.search(r'href="/picking\?tab=reports[^"]*#paMissing" id="qaMissing"', adm2) is not None and 'id="qaDay"' in adm2)

# ---- 19. Minimal admin: secondary panels are folded to a title row ---------------
check("19.1 Eleven secondary panels start folded", adm2.count("data-min-default") == 11, str(adm2.count("data-min-default")))
check("19.2 The main panel of each tab stays open",
      not re.search(r'data-min-default[^>]*>\s*<div class="card-header">\s*<div class="card-title">\s*Staff Management', adm2)
      and not re.search(r'data-min-default[^>]*id="pickingToday"', adm2))
_ux = open(os.path.join(REPO, "static", "js", "ux.js")).read()
check("19.3 A folded panel opens by tapping its title, and when a button jumps to it",
      'hasAttribute("data-min-default")' in _ux and "_mzOpen" in _ux and "openTarget" in _ux)

# ---- 20. Small fixes: bell, repeat bill number, remarks, quantity, colours -----
_dash = open(os.path.join(REPO, "templates", "dashboard.html")).read()
check("20.1 The notification list is moved out of the scrolling top bar, so the bell opens it on phones",
      "document.body.appendChild(dd)" in _dash and "function place()" in _dash and "#notif-dropdown { position: fixed !important" not in _dash)
c_a4 = login("st_asg@x.com")
ex = c_a4.get("/pick/bills/exists.json?no=8704").get_json()
check("20.2 A bill number already entered is reported as used (with when)", ex["exists"] is True and ex["bill_no"].endswith("8704") and ex["when"])
check("20.3 ...also with U typed after it", c_a4.get("/pick/bills/exists.json?no=8704u").get_json()["exists"] is True)
check("20.4 A new number is free", c_a4.get("/pick/bills/exists.json?no=999001").get_json()["exists"] is False)
check("20.5 Pickers cannot ask", login("st_om@x.com").get("/pick/bills/exists.json?no=8704").status_code == 403)
_pk3 = open(os.path.join(REPO, "templates", "_picking.html")).read()
check("20.6 The cursor stays on a repeat bill number: checked on typing, Enter and on tapping quantity",
      "function dupCheck" in _pk3 and 'addEventListener("focusin"' in _pk3 and "dupShow(d); return;" in _pk3)
with app.app_context():
    PickClaim.query.filter_by(status="active").update({"status": "cancelled"})
    db.session.commit()
    qb = A.add_pick_bill("8801", 4, ASG)
    qc = A.claim_items(RIA, qb.id, 4)
    A.finish_claim(RIA, qc.id, picked=None, missing=[("Betadine", 0), ("Zincovit", None)])
    rows = PickMissing.query.filter_by(claim_id=qc.id).order_by(PickMissing.id).all()
    check("20.7 A missing item with no quantity counts as 1", [(m.item_name, m.qty) for m in rows] == [("Betadine", 1), ("Zincovit", 1)]
          and db.session.get(PickClaim, qc.id).picked == 2, str([(m.item_name, m.qty) for m in rows]))
    M1, M2 = rows[0].id, rows[1].id
c_p4 = login("st_pur@x.com")
H = {"X-Requested-With": "fetch"}
r = c_p4.post("/pick/missing/%d/remark" % M1, json={"remark": "Ordered, comes tomorrow"}, headers=H).get_json()
check("20.8 Purchaser saves a remark; the item stays open with the remark shown",
      r["ok"] and any(m["id"] == M1 and m["remark"] == "Ordered, comes tomorrow" for m in r["items"]), str(r)[:200])
check("20.9 Only a purchaser (or admin) can add a remark",
      login("st_ria@x.com").post("/pick/missing/%d/remark" % M1, json={"remark": "x"}, headers=H).status_code == 403
      and c_a4.post("/pick/missing/%d/remark" % M1, json={"remark": "x"}, headers=H).status_code == 403)
c_p4.post("/pick/missing/%d/unavailable" % M2, json={"remark": "Company stopped it"}, headers=H)
with app.app_context():
    rep_ = A.pick_report(date.today())
    rm = {m["item"]: m for m in rep_["missing"]}
    check("20.10 The admin's missing-items report shows the purchaser's remarks",
          rm["Betadine"]["remark"] == "Ordered, comes tomorrow" and rm["Zincovit"]["remark"] == "Company stopped it"
          and rm["Zincovit"]["finished_by"] == "ST Pur")
    check("20.11 Every purchaser is alerted about missing items",
          {e.id for e in Employee.query.filter_by(staff_type="purchaser").all() if not e.is_admin} <= {e.id for e in A._purchasers()})
pgm = c_adm.get("/picking?tab=reports").get_data(as_text=True)
check("20.12 Remarks are on the report page and in the CSV", "Purchaser remark" in pgm and "Ordered, comes tomorrow" in pgm
      and "Company stopped it" in c_adm.get("/picking/report.csv").get_data(as_text=True))
_pm = open(os.path.join(REPO, "templates", "_pur_missing.html")).read()
check("20.13 The purchaser's box has a remark field and says all purchasers see it", 'data-pm="remark"' in _pm and "Every purchaser sees this list" in _pm)
check("20.14 Quantity box is wide enough for its label; free pickers are green; delete is a bin",
      'placeholder="Quantity"' in _pk3 and ".pk-chip.free{background:#e6f4ec" in _pk3 and "#3b5bdb" not in _pk3
      and "var BIN = " in _pk3 and ">×</button>" not in _pk3)

# ---- 21. A number tile opens its own report ------------------------------------
check("21.1 Picking page: each number links to its section",
      all(('#%s"' % h) in pgm for h in ("paBills", "paPickers", "paMissing", "paEvents")) and 'id="paPickers"' in pgm and 'id="paSum"' in pgm)
ur = c_adm.get("/picking?tab=reports&period=today&urgent=1").get_data(as_text=True)
check("21.2 Urgent bills opens a report of only urgent bills", "<h2>Urgent bills" in ur and "show all bills" in ur)
adm3 = c_adm.get("/admin_dashboard").get_data(as_text=True)
check("21.3 Admin: Picking today numbers open their reports", adm3.count('class="pt-tile"') == 8 and "#paMissing" in adm3 and "#paEvents" in adm3)
check("21.4 Admin: reports are grouped under a Reports heading",
      'id="qaReportsHead">Reports' in adm3 and all(('id="%s"' % i) in adm3 for i in ("qaUrgent", "qaLate", "qaBoard", "qaMissing", "qaStaffToday", "qaReports", "qaMulti", "qaDay")))
check("21.5 Admin: Total Staff and Active Today are links", 'href="#staff">Total Staff' in adm3 and 'href="/admin/staff-today">Active Today' in adm3)
st_pg = c_adm.get("/admin/staff-today").get_data(as_text=True)
check("21.6 Staff today: each number opens its list", 'href="#stWhoBox"' in st_pg and "#paMissing" in st_pg and 'id="stPicks"' in st_pg)
check("21.7 Assigner and picker number tiles are buttons that open their list",
      'data-go-tab="done"' in _pk3 and 'data-go-el="pkBillsWrap"' in _pk3 and _pk3.count('class="pk-stat" data-go-tab="mine"') == 5)
for u in ("/picking?tab=reports&period=today&urgent=1", "/picking?tab=reports&period=today", "/picking?tab=board&period=today",
          "/picking?tab=reports&period=month", "/admin/day", "/admin/staff-today", "/pick/me"):
    check("21.8 %s opens" % u, c_adm.get(u).status_code == 200)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
