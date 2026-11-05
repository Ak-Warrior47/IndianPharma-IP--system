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
from tests_helper import csrf_client  # noqa: E402
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

# ---- 7. The Jinja trap that broke the checker pages once ---------------
for t in ("staff_detail.html", "dashboard.html"):
    src = open(os.path.join(REPO, "templates", t)).read()
    check("7.x %s never reads a dict key called .items" % t, "period.items" not in src)

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
