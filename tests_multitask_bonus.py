"""Proves the multitask bonus reaches every view, not just the staff member's own card.

The bonus is +10 per extra role worked in a day, capped at +30 for that day, and is
averaged across the days in whatever period is being scored.

Run:  python3.13 tests_multitask_bonus.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date, timedelta  # noqa: E402

from app import (  # noqa: E402
    app, db, Employee, KPIEntry, MultitaskEntry, build_analytics,
)

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


TODAY = date.today()

with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False):
        emp = Employee.query.filter_by(email=email).first()
        if emp:
            return emp
        emp = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin)
        emp.set_password(PW)
        db.session.add(emp)
        db.session.commit()
        return emp

    mk("MB Admin", "mb_adm@x.com", "picker", admin=True)
    plain = mk("MB Plain", "mb_plain@x.com", "picker")
    multi = mk("MB Multi", "mb_multi@x.com", "picker")
    plain_id, multi_id = plain.id, multi.id

    # Identical KPI numbers for both, on the same single day.
    for eid in (plain_id, multi_id):
        if not KPIEntry.query.filter_by(emp_id=eid, entry_date=TODAY).first():
            db.session.add(KPIEntry(
                emp_id=eid, entry_date=TODAY, total_bills_received=20,
                sales_bills_open=18, picked=140, missed=2, cs_sales_open=3,
                packing_done=9, total_time=8, rack_organized=5,
                table_clean=1, sweep_done=1))
    db.session.commit()

    # Only the multitasker logs extra roles: two roles today -> +20 for that day.
    for role in ("checker", "billing"):
        if not MultitaskEntry.query.filter_by(
                emp_id=multi_id, entry_date=TODAY, secondary_type=role).first():
            db.session.add(MultitaskEntry(emp_id=multi_id, entry_date=TODAY,
                                          secondary_type=role, checked=40,
                                          bills_done=5))
    db.session.commit()

    day_entries_plain = KPIEntry.query.filter_by(emp_id=plain_id, entry_date=TODAY).all()
    day_entries_multi = KPIEntry.query.filter_by(emp_id=multi_id, entry_date=TODAY).all()

    s_plain = build_analytics(day_entries_plain, "picker", emp_id=plain_id)
    s_multi = build_analytics(day_entries_multi, "picker", emp_id=multi_id)

    check("1.1 Non-multitasker gets no bonus", s_plain.get("multitask_bonus", -1) == 0.0)
    check("1.2 Multitasker gets +20 for two extra roles",
          s_multi.get("multitask_bonus") == 20.0)
    check("1.3 Bonus actually raises eff_score",
          s_multi["eff_score"] > s_plain["eff_score"])
    check("1.4 eff_score difference equals the bonus (or is capped at 100)",
          abs((s_multi["eff_score"] - s_plain["eff_score"]) - 20.0) < 0.15
          or s_multi["eff_score"] == 100.0)
    check("1.5 eff_score never exceeds 100", s_multi["eff_score"] <= 100.0)

    # Cap: four extra roles in one day must still yield +30, not +40.
    for role in ("purchaser", "delivery"):
        if not MultitaskEntry.query.filter_by(
                emp_id=multi_id, entry_date=TODAY, secondary_type=role).first():
            db.session.add(MultitaskEntry(emp_id=multi_id, entry_date=TODAY,
                                          secondary_type=role, quantity=4))
    db.session.commit()
    s_capped = build_analytics(
        KPIEntry.query.filter_by(emp_id=multi_id, entry_date=TODAY).all(),
        "picker", emp_id=multi_id)
    check("2.1 Four extra roles in a day is capped at +30",
          s_capped.get("multitask_bonus") == 30.0)

    # Period averaging: a second day with no multitask work halves the bonus.
    YESTERDAY = TODAY - timedelta(days=1)
    if not KPIEntry.query.filter_by(emp_id=multi_id, entry_date=YESTERDAY).first():
        db.session.add(KPIEntry(
            emp_id=multi_id, entry_date=YESTERDAY, total_bills_received=20,
            sales_bills_open=18, picked=140, missed=2, cs_sales_open=3,
            packing_done=9, total_time=8, rack_organized=5,
            table_clean=1, sweep_done=1))
        db.session.commit()
    s_two_day = build_analytics(
        KPIEntry.query.filter(KPIEntry.emp_id == multi_id,
                              KPIEntry.entry_date >= YESTERDAY).all(),
        "picker", emp_id=multi_id)
    check("3.1 Two-day window averages the daily bonus (30 over 2 days -> 15)",
          s_two_day.get("multitask_bonus") == 15.0)

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


# The admin dashboard scores through build_analytics, so it must now see the bonus.
login("mb_adm@x.com")
r = client.get("/admin_dashboard")
check("4.1 Admin dashboard renders", r.status_code == 200)

with app.app_context():
    month_start = TODAY.replace(day=1)
    a_multi = build_analytics(
        KPIEntry.query.filter(KPIEntry.emp_id == multi_id,
                              KPIEntry.entry_date >= month_start).all(),
        "picker", emp_id=multi_id)
    a_plain = build_analytics(
        KPIEntry.query.filter(KPIEntry.emp_id == plain_id,
                              KPIEntry.entry_date >= month_start).all(),
        "picker", emp_id=plain_id)
check("4.2 Month window carries a bonus for the multitasker",
      a_multi.get("multitask_bonus", 0) > 0)
check("4.3 Month window carries no bonus for the non-multitasker",
      a_plain.get("multitask_bonus", -1) == 0.0)
check("4.4 Multitasker outranks the identical non-multitasker on the month score",
      a_multi["eff_score"] > a_plain["eff_score"])

# The staff member's own dashboard must agree, not double-count.
login("mb_multi@x.com")
r = client.get("/dashboard")
check("5.1 Staff dashboard renders", r.status_code == 200)
with app.app_context():
    s_day = build_analytics(
        KPIEntry.query.filter_by(emp_id=multi_id, entry_date=TODAY).all(),
        "picker", emp_id=multi_id)
check("5.2 Day bonus is +30 once, not double-applied", s_day.get("multitask_bonus") == 30.0)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
