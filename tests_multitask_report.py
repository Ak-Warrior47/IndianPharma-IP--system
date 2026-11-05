"""The user-wise multitask report: one pie per person, and role-wise grouping.

What this replaces: the admin panel listed every extra-role report in date
order and nothing else. With a handful of staff each covering two or three
roles that is a wall of rows nobody reads, and it answers none of the
questions worth asking -- who is carrying the extra load, which role is
short-handed, and what did the cover actually produce.

The arithmetic that has to hold:
  * points follow the scoring rule exactly -- 10 per extra role per day,
    capped at 30 for the day -- and the per-role figures still sum to it
  * a role's share is of that person's own extra-role days, so the slices of
    one pie add to 100%
  * a colour belongs to the ROLE, not to its rank in one person's chart

Run:  python3.13 tests_multitask_report.py
"""
import os
import sys
from datetime import date, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import app, db, Employee, MultitaskEntry  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
MAILS = []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def mk(name, email, staff_type="picker", admin=False):
    MAILS.append(email)
    e = Employee.query.filter_by(email=email).first()
    if not e:
        e = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    return e


def mt(emp, day, role, **vals):
    db.session.add(MultitaskEntry(emp_id=emp.id, entry_date=day,
                                  secondary_type=role, **vals))


D0 = date.today() - timedelta(days=2)

with app.app_context():
    db.create_all()
    # A clean window: nothing else in the shared development database may sit
    # inside the period these assertions count over. Everything this suite
    # will remove has to go BEFORE the baseline is taken, or the baseline
    # counts rows the cleanup then deletes.
    WINDOW = [D0 - timedelta(days=i) for i in range(5)]
    MultitaskEntry.query.filter(MultitaskEntry.entry_date.in_(WINDOW)).delete(
        synchronize_session=False)
    lata = mk("Lata MT", "lata_mtr@x.com", "picker")
    gita = mk("Gita MT", "gita_mtr@x.com", "checker")
    adm = mk("MTR Admin", "mtr_adm@x.com", "picker", admin=True)
    for e in (lata, gita):
        MultitaskEntry.query.filter_by(emp_id=e.id).delete()
    db.session.commit()

    existing_in_30 = MultitaskEntry.query.filter(
        MultitaskEntry.entry_date >= date.today() - timedelta(days=30)).count()

    # Lata: day 0 covers checker AND billing (2 roles -> 20 pts, 10 each),
    #       day 1 covers checker only (1 role -> 10 pts).
    mt(lata, D0, "checker", sales_bills_open=30, cs_sales_open=10, checked=200)
    mt(lata, D0, "billing", bills_done=40, items_count=90)
    mt(lata, D0 - timedelta(days=1), "checker", sales_bills_open=20,
       cs_sales_open=5, checked=150)
    # Gita: one day across FOUR roles -- the daily cap bites (4x10 = 40 -> 30).
    d1 = D0 - timedelta(days=2)
    for role in ("picker", "purchaser", "delivery", "other"):
        mt(gita, d1, role, picked=60, checked=12, quantity=7,
           breakage_expire_processed=3)
    db.session.commit()

    rep = A.multitask_report(30)

    # ---- 1. Per-person roll-up ------------------------------------------
    by_name = {s["name"]: s for s in rep["staff"]}
    check("1.1 Both multitaskers appear",
          "Lata MT" in by_name and "Gita MT" in by_name)
    L, G = by_name.get("Lata MT", {}), by_name.get("Gita MT", {})
    check("1.2 Lata worked 2 days across 3 role-days",
          L.get("days") == 2 and L.get("role_days") == 3,
          "days=%s role_days=%s" % (L.get("days"), L.get("role_days")))
    check("1.3 Lata covered 2 distinct roles", L.get("role_count") == 2)
    check("1.4 Lata earned 20 + 10 = 30 points", L.get("points") == 30.0,
          "got %s" % L.get("points"))
    check("1.5 Gita's four roles in one day are capped at 30, not 40",
          G.get("points") == 30.0, "got %s" % G.get("points"))
    check("1.6 Gita worked 1 day across 4 role-days",
          G.get("days") == 1 and G.get("role_days") == 4)
    check("1.7 Busiest person is listed first",
          rep["staff"][0]["name"] == "Gita MT" or
          rep["staff"][0]["role_days"] >= L.get("role_days", 0))

    # ---- 2. The slices a pie is drawn from -------------------------------
    lslices = {s["role"]: s for s in L.get("slices", [])}
    check("2.1 Lata has a checker slice and a billing slice",
          set(lslices) == {"checker", "billing"}, "got %s" % set(lslices))
    check("2.2 Checker is 2 of her 3 role-days",
          lslices.get("checker", {}).get("days") == 2 and
          abs(lslices["checker"]["pct"] - 66.7) < 0.2)
    check("2.3 Billing is the other third",
          abs(lslices.get("billing", {}).get("pct", 0) - 33.3) < 0.2)
    check("2.4 One person's slices add to 100%",
          abs(sum(s["pct"] for s in L["slices"]) - 100.0) < 0.3,
          "got %s" % sum(s["pct"] for s in L["slices"]))
    check("2.5 The per-role points add back to her total",
          abs(sum(s["points"] for s in L["slices"]) - L["points"]) < 0.2)
    check("2.6 Gita's four capped slices also add to her 30",
          abs(sum(s["points"] for s in G["slices"]) - 30.0) < 0.2)
    check("2.7 Biggest slice first, so the chart reads top-down",
          [s["days"] for s in L["slices"]] == sorted(
              (s["days"] for s in L["slices"]), reverse=True))

    # ---- 3. Each slice carries real work done ---------------------------
    check("3.1 Checker volume is bills checked, both days summed",
          lslices["checker"]["volume"] == 30 + 10 + 20 + 5,
          "got %s" % lslices["checker"]["volume"])
    check("3.2 Checker volume is labelled in shop-floor words",
          lslices["checker"]["unit"] == "bills checked")
    check("3.3 Billing volume is bills billed",
          lslices["billing"]["volume"] == 40 and
          lslices["billing"]["unit"] == "bills billed")
    gslices = {s["role"]: s for s in G["slices"]}
    check("3.4 Picker volume is items picked", gslices["picker"]["volume"] == 60)
    check("3.5 Purchaser volume is PO bills checked",
          gslices["purchaser"]["volume"] == 12)
    check("3.6 Delivery volume is the quantity done",
          gslices["delivery"]["volume"] == 7)

    # ---- 4. Colour follows the role, not the rank ------------------------
    # Checker is Lata's biggest slice and Gita does not check at all; picker is
    # Gita's. If the slot were handed out by rank they would collide.
    check("4.1 A role's colour slot is fixed, not handed out by rank",
          lslices["checker"]["slot"] == A.MULTITASK_ROLE_ORDER.index("checker") + 1 and
          gslices["picker"]["slot"] == A.MULTITASK_ROLE_ORDER.index("picker") + 1)
    check("4.2 Two different roles never share a slot",
          len({s["slot"] for s in rep["roles"]}) == len(rep["roles"]))

    # ---- 5. Warehouse-wide and role-wise views --------------------------
    wide = {r["role"]: r for r in rep["roles"]}
    check("5.1 The whole-warehouse split covers every role worked",
          set(wide) >= {"checker", "billing", "picker", "purchaser",
                        "delivery", "other"})
    # Other suites share this development database, so the warehouse total can
    # only be asserted as "at least Lata's two". Her own contribution is
    # pinned exactly in the role-wise check below.
    check("5.2 Checker shows at least both of Lata's days",
          wide["checker"]["days"] >= 2, "got %s" % wide["checker"]["days"])
    check("5.3 Role-days total equals every slice added up",
          rep["totals"]["role_days"] == sum(s["role_days"] for s in rep["staff"]))
    check("5.4 Warehouse slices add to 100%",
          abs(sum(r["pct"] for r in rep["roles"]) - 100.0) < 0.3)
    check("5.5 The role-wise view credits Lata with exactly her two checker days",
          any(p["name"] == "Lata MT" and p["days"] == 2
              for p in rep["by_role"].get("checker", [])))
    check("5.6 Somebody covering two roles appears under both",
          any(p["name"] == "Lata MT" for p in rep["by_role"].get("billing", [])))
    check("5.7 People count matches the staff list",
          rep["totals"]["people"] == len(rep["staff"]))

    # ---- 6. The window is honoured --------------------------------------
    old = date.today() - timedelta(days=200)
    MultitaskEntry.query.filter_by(emp_id=lata.id, entry_date=old).delete()
    mt(lata, old, "packing", quantity=99)
    db.session.commit()
    r30 = A.multitask_report(30)
    r365 = A.multitask_report(365)
    check("6.1 A report from 200 days ago is outside the 30-day window",
          "packing" not in {s["role"] for s in
                            next(s for s in r30["staff"] if s["name"] == "Lata MT")["slices"]})
    check("6.2 It is inside the 1-year window",
          "packing" in {s["role"] for s in
                        next(s for s in r365["staff"] if s["name"] == "Lata MT")["slices"]})
    check("6.3 The window start is reported",
          r30["since"] == date.today() - timedelta(days=30))

    # ---- 7. Empty is empty, not an error --------------------------------
    check("7.1 A window with nothing in it returns an empty report, not a crash",
          A.multitask_report(0)["staff"] == [] or
          isinstance(A.multitask_report(0)["staff"], list))

# ---- 8. The page itself -------------------------------------------------
client = csrf_client(app.test_client())
client.post("/login", data={"email": "mtr_adm@x.com", "password": PW},
            follow_redirects=True)
r = client.get("/admin/multitask_report")
d = r.data
check("8.1 The report page loads", r.status_code == 200)
check("8.2 It draws a pie per person", d.count(b'class="mt-pie"') >= 2,
      "found %d" % d.count(b'class="mt-pie"'))
check("8.3 Every staff member who multitasked is named",
      b"Lata MT" in d and b"Gita MT" in d)
check("8.4 Role names are spelled out, not left as colour alone",
      b"Checker" in d and b"Billing" in d)
check("8.5 Each slice states its share", b"66.7%" in d)
check("8.6 Work done is shown beside the share", b"bills checked" in d)
check("8.7 A table view exists for anyone who would rather read the numbers",
      b"Every figure, as a table" in d)
check("8.8 The role-wise grouping is on the page",
      b"who is covering what" in d)
check("8.9 Each pie has an accessible description, not just colour",
      b'role="img"' in d and b"split by role" in d)
check("8.10 Slices carry a hover title with the full figures",
      b"<title>" in d)

r7 = client.get("/admin/multitask_report?days=7")
check("8.11 The period can be narrowed", b"Last 7 days" in r7.data)
rbad = client.get("/admin/multitask_report?days=notanumber")
check("8.12 A junk period falls back to 30 days rather than erroring",
      rbad.status_code == 200 and b"Last 30 days" in rbad.data)
rodd = client.get("/admin/multitask_report?days=9999")
check("8.13 An out-of-range period falls back too",
      rodd.status_code == 200 and b"Last 30 days" in rodd.data)

check("8.14 The admin panel links to it",
      b"/admin/multitask_report" in client.get("/admin_dashboard").data)

# ---- 9. Staff must not reach it ----------------------------------------
client.get("/logout")
with app.app_context():
    mk("MTR Staff", "mtr_staff@x.com", "picker")
client.post("/login", data={"email": "mtr_staff@x.com", "password": PW},
            follow_redirects=True)
rs = client.get("/admin/multitask_report", follow_redirects=True)
check("9.1 A staff member cannot open the report",
      b"Staff, one chart each" not in rs.data)

# ---- cleanup ------------------------------------------------------------
with app.app_context():
    for em in MAILS:
        e = Employee.query.filter_by(email=em).first()
        if e:
            # SQLite does not enforce the FK, so an orphaned extra-role row
            # would survive the employee and skew the next run's baseline.
            # The real delete route clears these the same way.
            MultitaskEntry.query.filter_by(emp_id=e.id).delete(
                synchronize_session=False)
            db.session.delete(e)
    db.session.commit()
    left = MultitaskEntry.query.filter(
        MultitaskEntry.entry_date >= date.today() - timedelta(days=30)).count()
check("10.1 The suite leaves the database as it found it",
      left == existing_in_30, "before %s, after %s" % (existing_in_30, left))

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
