"""The owner's list: day view, best-day card, explanation button, minimize,
role-wise multitask, rising targets, and the fast "Close All Days".

Run:  python3.13 tests_owner_list.py
"""
import os
import sys
from datetime import date, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import app, db, Employee, KPIEntry, MultitaskEntry, PastEntryWindow  # noqa: E402
from tests_helper import csrf_client, add_picks  # noqa: E402
from sqlalchemy import event  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
TODAY = date.today()
MAILS = ["ol_adm@x.com", "ol_chk@x.com", "ol_pick@x.com"]


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def mk(name, email, st, admin=False, sec=None):
    e = Employee.query.filter_by(email=email).first()
    if not e:
        e = Employee(name=name, email=email, staff_type=st, is_admin=admin, secondary_staff_type=sec)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    return e


# Two weeks of working days before today. Some may fall in last month -- the
# best-day card only counts this month, the target needs at least five days.
DAYS = [TODAY - timedelta(days=i) for i in range(1, 17)]
DAYS = [d for d in DAYS if d.weekday() != 6][:12]
CUR = [i for i, d in enumerate(DAYS) if d >= TODAY.replace(day=1)]

with app.app_context():
    db.create_all()
    adm = mk("OL Admin", "ol_adm@x.com", "picker", admin=True)
    chk = mk("OL Checker", "ol_chk@x.com", "checker")
    pk = mk("OL Picker", "ol_pick@x.com", "picker", sec="checker")
    for e in (chk, pk):
        KPIEntry.query.filter_by(emp_id=e.id).delete()
        MultitaskEntry.query.filter_by(emp_id=e.id).delete()
        e.custom_hourly_target = None
    db.session.commit()
    for i, d in enumerate(DAYS):
        db.session.add(KPIEntry(emp_id=chk.id, entry_date=d, bills_received=200,
                                sales_bills_open=20 + i, cs_sales_open=5,
                                checked=100 + 10 * i, errors_found=20, total_time=9, check_time=9))
        db.session.add(KPIEntry(emp_id=pk.id, entry_date=d, total_bills_received=40,
                                sales_bills_open=35, picked=150 + i, missed=1, total_time=9))
    # picker covered checker (and on day 0 also delivery) as a second role
    db.session.add(MultitaskEntry(emp_id=pk.id, entry_date=DAYS[0], secondary_type="checker",
                                  bills_received=200, sales_bills_open=4))
    db.session.add(MultitaskEntry(emp_id=pk.id, entry_date=DAYS[0], secondary_type="delivery", quantity=6))
    db.session.commit()
    # Bill-based picking: the same days as finished grabs (about 17 items/hr).
    for i, d in enumerate(DAYS):
        add_picks(pk.id, d, 150 + i, hours=9.0)
    CHK, PK = chk.id, pk.id

    # ---- 1. Checker's best day --------------------------------------------
    b = A.checker_month_bests(CHK)
    top_bills = max(20 + i + 5 for i in CUR) if CUR else 0
    top_items = max(100 + 10 * i + 20 for i in CUR) if CUR else 0
    check("1.1 Best bills day this month is found",
          (b["this"]["bills"] or {}).get("value", 0) == top_bills, "got %s" % b["this"]["bills"])
    check("1.2 Best items day this month is found",
          (b["this"]["items"] or {}).get("value", 0) == top_items)
    check("1.3 The day-wise list covers every day this month", len(b["days"]) == len(CUR))
    check("1.3b Last month's best is kept as the old record",
          len(CUR) == len(DAYS) or b["prev"]["bills"] is not None)
    check("1.4 With no entry today, the message names the record to beat",
          ("Beat it today" in b["message"] and str(top_bills) in b["message"]) if CUR
          else "Last month" in b["message"], b["message"])
    db.session.add(KPIEntry(emp_id=CHK, entry_date=TODAY, bills_received=200,
                            sales_bills_open=top_bills + 5, cs_sales_open=0,
                            checked=50, errors_found=0, total_time=9, check_time=9))
    db.session.commit()
    b2 = A.checker_month_bests(CHK)
    check("1.5 Beating it today says so",
          (b2["record"] and "New record" in b2["message"]) if CUR else True, b2["message"])
    KPIEntry.query.filter_by(emp_id=CHK, entry_date=TODAY).delete()
    db.session.commit()

    # ---- 2. Multitask explanation, day by day ---------------------------
    st = A.build_analytics(KPIEntry.query.filter_by(emp_id=PK).all(), "picker", emp_id=PK)
    bd = st["multitask_breakdown"]
    check("2.1 One row per multitask day", len(bd) == 1, "got %s" % bd)
    check("2.2 It lists both roles and 2 x 10 = 20 points",
          bd and bd[0]["roles"] == ["checker", "delivery"] and bd[0]["points"] == 20)
    check("2.3 The rows add to the day points behind the bonus",
          sum(r["points"] for r in bd) == st["multitask_day_points"])

    # ---- 3. Targets that rise with performance --------------------------
    sug = st["target_suggestion"]
    check("3.1 A picker gets a target suggestion from their own pace", bool(sug), str(sug))
    check("3.2 With an unreachable role target, it proposes a personal one",
          sug and sug["kind"] == "set" and sug["suggested"] < sug["current"])
    cs = A.build_analytics(KPIEntry.query.filter_by(emp_id=CHK).all(), "checker", emp_id=CHK)
    check("3.3 A checker now gets a suggestion too (it never could before)",
          bool(cs["target_suggestion"]))

# ---- 4. Pages --------------------------------------------------------------
c = csrf_client(app.test_client())
c.post("/login", data={"email": "ol_adm@x.com", "password": PW}, follow_redirects=True)

r = c.get("/admin/day?date=%s" % DAYS[0].isoformat())
check("4.1 Day view loads for a chosen date", r.status_code == 200 and b"OL Checker" in r.data)
check("4.2 It shows the checking team's pending for that day", b"Pending" in r.data)
check("4.3 It links to the previous and next days", b"\xe2\x86\x90" in r.data or b"&larr;" in r.data)
check("4.4 A junk date falls back to the latest day, not an error",
      c.get("/admin/day?date=nonsense").status_code == 200)

d = c.get("/admin_dashboard").data
check("4.5 Admin staff table shows Today, Week and Month",
      b"Today<span" in d and b"Week<span" in d and b"Month<span" in d)
check("4.6 Role-wise multitask is on the dashboard itself", b"mt-rolewise__row" in d)
check("4.7 Day View is reachable from the dashboard", b"/admin/day" in d)

p = c.get("/staff/%d" % PK).data
check("4.8 Staff profile has the multitask explanation button",
      b"How were these multitask points added?" in p)
check("4.9 Staff profile offers the target change as one click", b"accept_target_suggestion" in p)
cp = c.get("/staff/%d" % CHK)
check("4.10 Checker profile renders with the best-day card",
      cp.status_code == 200 and b"Best Day This Month" in cp.data)

c.post("/admin/accept_target_suggestion/%d" % PK, follow_redirects=True)
with app.app_context():
    t = db.session.get(Employee, PK).custom_hourly_target
check("4.11 Applying the suggestion sets the personal target", t == sug["suggested"], str(t))

mr = c.get("/admin/multitask_report").data
check("4.12 Everyone on the multitask report gets a pie, even with one role",
      b"mt-solo" not in mr and mr.count(b'class="mt-pie"') >= 2)

# ---- 5. Close All Days is a handful of queries, not one per day -------
months = ["%d-%02d" % (TODAY.year + ((TODAY.month - 1 - i) // 12), (TODAY.month - 1 - i) % 12 + 1)
          for i in range(1, 4)]
Q = [0]
with app.app_context():
    event.listen(db.engine, "before_cursor_execute", lambda *a, **k: Q.__setitem__(0, Q[0] + 1))
c.post("/admin/past_window_bulk", data={"months": months, "action": "open"})
Q[0] = 0
c.post("/admin/past_window_bulk", data={"months": months, "action": "close"})
close_q = Q[0]
with app.app_context():
    _lo = date(int(months[-1][:4]), int(months[-1][5:]), 1)
    _hi = TODAY.replace(day=1)
    still = PastEntryWindow.query.filter(PastEntryWindow.is_active.is_(True),
                                         PastEntryWindow.past_date >= _lo,
                                         PastEntryWindow.past_date < _hi).count()
check("5.1 Closing three months takes under 10 queries (%d)" % close_q, close_q < 10)
check("5.2 And every window really is closed", still == 0, "%d still open" % still)

# ---- 5a. Close All Days with no month picked closes everything open ---
with app.app_context():
    for d in (TODAY - timedelta(days=3), TODAY - timedelta(days=40), TODAY - timedelta(days=90)):
        w = PastEntryWindow.query.filter_by(past_date=d).first()
        if w: w.is_active = True
        else: db.session.add(PastEntryWindow(past_date=d, is_active=True))
    db.session.commit()
r = c.post("/admin/past_window_bulk", data={"action": "close"}, follow_redirects=True)
with app.app_context():
    left = PastEntryWindow.query.filter(PastEntryWindow.is_active.is_(True)).count()
check("5.2b Close All Days with no month picked closes every open day", left == 0, "%d open" % left)
check("5.2c It says how many it closed", b"Closed all" in r.data or b"No past-entry days" in r.data)
check("5.2d The open-windows row has a one-click Close all button",
      b"Close all" in open(os.path.join(REPO, "templates", "admin.html"), "rb").read())

# ---- 5b. Page loads do not re-ask the database per staff member --------
# The admin dashboard used to make ~3 queries per staff member per period
# (open complaints, supervisor strikes, multitask), 97 for 23 staff.
with app.app_context():
    n_staff = Employee.query.filter_by(is_admin=False).count()
Q[0] = 0
c.get("/admin_dashboard")
adq = Q[0]
check("5.3 Admin dashboard stays near one query per staff member (%d for %d staff)"
      % (adq, n_staff), adq <= 2 * n_staff + 20)
Q[0] = 0
c.get("/staff/%d" % CHK)
spq = Q[0]
check("5.4 Staff profile stays near one query per colleague (%d for %d staff)"
      % (spq, n_staff), spq <= 2 * n_staff + 25)

# ---- 6. Minimize is wired on every page that uses the shared script ----
ux = open(os.path.join(REPO, "static", "js", "ux.js")).read()
css = open(os.path.join(REPO, "static", "css", "ip.css")).read()
check("6.1 The shared script adds a minimize button to cards", "mz-btn" in ux and "is-min" in ux)
check("6.2 Minimized cards fold to their title", ".is-min > :not(.mz-head)" in css)
check("6.3 The staff profile loads the shared script", b"js/ux.js" in p)

# ---- 6b. Second-pass gaps -------------------------------------------------
with app.app_context():
    # supervisors: extra roles explained even with no score
    sup = mk("OL Sup", "ol_sup@x.com", "supervisor"); MAILS.append("ol_sup@x.com")
    MultitaskEntry.query.filter_by(emp_id=sup.id).delete()
    db.session.add(MultitaskEntry(emp_id=sup.id, entry_date=DAYS[0], secondary_type="picker", picked=40))
    db.session.commit(); SUP = sup.id
    md = A.multitask_days_for(SUP)
    check("6b.1 Multitask days are found for someone with no KPI entries", len(md) == 1 and md[0]["points"] == 10)
    # second-role checker gets a best-day record from their checker-role rows
    pb = A.checker_month_bests(PK)
    check("6b.2 Staff who check as a second role get a best-day record",
          pb["this"]["bills"] is not None or pb["prev"]["bills"] is not None)
    # targets rise automatically, only up, only for personal targets
    e = db.session.get(Employee, PK); e.custom_hourly_target = 5.0; db.session.commit()
    raised = A.auto_raise_targets()
    e = db.session.get(Employee, PK)
    check("6b.3 The monthly job raises a personal target when pace has risen", e.custom_hourly_target > 5.0)
    check("6b.4 Running it twice changes nothing more", A.auto_raise_targets() == [])
    check("6b.5 It is scheduled monthly", "id=\"auto_raise_targets\"" in open(os.path.join(REPO, "app.py")).read())
sp = c.get("/staff/%d" % SUP).data
check("6b.6 Supervisor profile has the multitask explanation button", b"How are multitask points added?" in sp)
# phones and charts
for t in ("dashboard.html", "past_entry.html"):
    src = open(os.path.join(REPO, "templates", t)).read()
    check("6b.7 %s banners span all columns, never a fixed 2" % t, "span 2" not in src)
for t in ("dashboard.html", "staff_detail.html", "admin_delivery.html"):
    src = open(os.path.join(REPO, "templates", t)).read()
    check("6b.8 %s loads charts from the app, not an outside site" % t,
          "js/chart.umd.min.js" in src and "cdn.jsdelivr.net/npm/chart.js" not in src
          and "cdnjs.cloudflare.com/ajax/libs/Chart.js" not in src)
    check("6b.9 %s cannot crash if charts fail to load" % t, "window.Chart && new Chart(" in src)
check("6b.10 The chart library is in the app", os.path.getsize(os.path.join(REPO, "static", "js", "chart.umd.min.js")) > 100000)

# ---- 6c. The Bill Validations page is gone, links and all ---------------
_rv = c.get("/validations", follow_redirects=True)
check("6c.1 /validations no longer exists (an old bookmark lands on the dashboard)",
      b"Bill Validations" not in _rv.data and b"Staff Management" in _rv.data)
import glob as _g
_left = [f for f in _g.glob(os.path.join(REPO, "templates", "*.html")) if "/validations" in open(f).read()]
check("6c.2 No page links to it (%s)" % [os.path.basename(f) for f in _left], not _left)
_src = open(os.path.join(REPO, "app.py")).read()
check("6c.3 Pickers' submissions no longer create validations", "bv = BillValidation(" not in _src)
check("6c.4 The overdue-validations SMS is no longer scheduled", "stale_validations_sms" not in _src)
check("6c.5 Past validation records are kept (table still defined)", "class BillValidation(db.Model)" in _src)

# ---- 6d. The PDF report ------------------------------------------------
import subprocess as _sp, tempfile as _tf
from utils import generate_visual_pdf as _gpdf
with app.app_context(), app.test_request_context():
    for _em in ("ol_chk@x.com", "ol_pick@x.com"):
        _e = Employee.query.filter_by(email=_em).first()
        _pl = A.build_pdf_payload(_e)
        _b = _gpdf(_e.name, _pl).getvalue()
        with _tf.NamedTemporaryFile(suffix=".pdf") as _f:
            _f.write(_b); _f.flush()
            _txt = _sp.run(["pdftotext", "-layout", _f.name, "-"], capture_output=True, text=True).stdout
        _r = _e.staff_type
        check("6d.1 %s PDF builds" % _r, len(_b) > 3000 and "could not be generated" not in _txt)
        check("6d.2 %s PDF has no Bill Validations section" % _r, "VALIDATION" not in _txt.upper())
        check("6d.3 %s PDF carries no old formula (speed / 80, typed pending)" % _r,
              "80 target" not in _txt and "Speed÷80" not in _txt and "SB Checked + Pending" not in _txt)
        check("6d.4 %s PDF never calls urgent work an error" % _r,
              "Error Density" not in _txt and "Clean Check" not in _txt)
        check("6d.5 %s PDF reconciles to the final score" % _r, "Final score" in _txt and "KPI points" in _txt)
        if _r == "checker":
            check("6d.6 checker PDF states the team pending rule", "Team pending" in _txt)
        check("6d.7 %s payload no longer carries validations" % _r, "pending_validations" not in _pl)

# ---- 6e. Same figure from everyone + over-checked = flagged, not multiplied
with app.app_context():
    _d = TODAY - timedelta(days=200)
    for _x in Employee.query.filter(Employee.email.in_(("ol_chk@x.com",))).all():
        KPIEntry.query.filter_by(emp_id=_x.id, entry_date=_d).delete()
        db.session.add(KPIEntry(emp_id=_x.id, entry_date=_d, bills_received=50,
                                sales_bills_open=40, cs_sales_open=20, total_time=9, check_time=9))
    db.session.commit()
    _row = A.checker_team_day_totals([_d])[_d]
    check("6e.1 One figure, more checked than it: flagged, never multiplied up",
          _row["mode"] == "conflict" and _row["total"] == 50, _row)

# ---- 6f. Deep scan: a part can never exceed its whole, for every role ---
ep = A.entry_problems
check("6f.1 Picker: bills picked over bills given is refused",
      bool(ep("picker", dict(total_bills_received=40, sales_bills_open=60, picked=200))))
check("6f.2 Picker: a normal day passes",
      ep("picker", dict(total_bills_received=40, sales_bills_open=35, picked=200, missed=2)) == [])
check("6f.3 Picker: more bills than items is refused",
      bool(ep("picker", dict(total_bills_received=40, sales_bills_open=30, picked=10))))
check("6f.4 Purchaser: PO checked over received is refused",
      bool(ep("purchaser", dict(sales_bills_open=10, checked=25))))
check("6f.5 Purchaser: PO entered over received is refused",
      bool(ep("purchaser", dict(sales_bills_open=10, picked=30))))
check("6f.6 Purchaser: racked over items is refused",
      bool(ep("purchaser", dict(sales_bills_open=10, errors_found=100, rack_organized=500))))
check("6f.7 Purchaser: CS received over CS open is refused",
      bool(ep("purchaser", dict(sales_bills_open=10, cs_sales_open=5, packing_done=20))))
check("6f.8 Checker: more bills than items is refused",
      bool(ep("checker", dict(sales_bills_open=20, cs_sales_open=5, checked=10))))
check("6f.9 Checker: an urgent-only day passes",
      ep("checker", dict(sales_bills_open=0, cs_sales_open=20, checked=0, errors_found=120)) == [])
_src = open(os.path.join(REPO, "app.py")).read()
check("6f.10 A checker with only urgent items is not told 'Items Checked' is missing",
      "checked + errors_found <= 0" in _src)
with app.app_context():
    _m = A.get_period_entries(CHK, "month")
    check("6f.11 'This month' is the calendar month on every screen",
          all(e.entry_date >= TODAY.replace(day=1) for e in _m))
    _n = A.DeliveryBillerNote(note_type="packer", quantity=12, issues_count=40)
    check("6f.12 A packer's figures are labelled as the packer form asked",
          _n.figures == "12 sales bills open · 40 bills received")
_adm = c.get("/admin_dashboard").data.decode()
check("6f.13 Performance Summary is titled with the month it shows", "Performance Summary — All Time" not in _adm)
check("6f.14 No checker card shows the score as 'Accuracy' or rewards 'Normal%'",
      "}}pts</td>" not in open(os.path.join(REPO, "templates", "admin.html")).read() and "Normal%" not in _adm)
_dash = open(os.path.join(REPO, "templates", "dashboard.html")).read()
check("6f.15 The checker dashboard no longer calls urgent work an 'Error rate'",
      "Clean Check Rate" not in _dash and "Error rate:" not in _dash)
check("6f.16 Second-role checkers are not asked to type pending", 'name="mt_c_pending"' not in _dash)

# ---- 7. The Jinja trap that broke the checker pages once ---------------
for t in ("staff_detail.html", "dashboard.html"):
    src = open(os.path.join(REPO, "templates", t)).read()
    check("7.x %s never reads a dict key called .items" % t, "period.items" not in src)

# ---- 8. An urgent BILL always earns more than a normal one -------------
# Found by a strict re-check: same 30 bills, 10 of them urgent, scored exactly
# the same as 30 normal, because the bills figure only counted when it was the
# weaker of items/day and bills/day.
from datetime import date as _d  # noqa: E402
with app.app_context(), app.test_request_context():
    def _ck(nb, ub, items=60, urgent_items=0):
        e = KPIEntry(emp_id=987654, entry_date=_d(2020, 1, 6), bills_received=100,
                     sales_bills_open=nb, cs_sales_open=ub, checked=items,
                     errors_found=urgent_items, total_time=9)
        return A.build_analytics([e], "checker", emp_id=987654)
    plain, urg = _ck(30, 0), _ck(20, 10)
    check("8.1 Same 30 bills, 10 urgent: the urgent day scores higher",
          urg["eff_score"] > plain["eff_score"], "%s vs %s" % (plain["eff_score"], urg["eff_score"]))
    check("8.2 Urgent items still score higher too",
          _ck(30, 0, 40, 20)["eff_score"] > _ck(30, 0, 60, 0)["eff_score"])
    _er = next(r for r in urg["point_breakdown"] if r["name"] == "Efficiency Ratio")
    check("8.3 The point line says urgent counts ×1.5 in bills", "35 bills/day" in _er["how"]
          and "1.5" in _er["how"], _er["how"])

with app.app_context():
    for em in MAILS:
        e = Employee.query.filter_by(email=em).first()
        if e:
            KPIEntry.query.filter_by(emp_id=e.id).delete()
            MultitaskEntry.query.filter_by(emp_id=e.id).delete()
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
