"""Scoring correctness: role-aware formulas and an auditable breakdown.

Two defects this pins down:

1. Only picker and purchaser had their own branch in build_analytics; every
   other staff_type fell through to the CHECKER formula. A supervisor who also
   picks was therefore scored with checker maths on picker numbers, producing a
   meaningless total and a false RE-TRAINING grade.

2. The score was a single opaque number. The multitask bonus was folded in with
   nothing on screen to show it had been applied, so from the admin's side the
   feature looked broken.

Run:  python3.13 tests_scoring.py
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

from tests_helper import csrf_client  # noqa: E402
app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
TODAY = date.today()


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


def picker_entry(eid, d):
    return KPIEntry(emp_id=eid, entry_date=d, total_bills_received=24,
                    sales_bills_open=22, picked=170, missed=3, cs_sales_open=3,
                    packing_done=11, total_time=8, rack_organized=9,
                    table_clean=1, sweep_done=1)


with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False, secondary=None):
        e = Employee.query.filter_by(email=email).first()
        if e:
            return e
        e = Employee(name=name, email=email, staff_type=staff_type,
                     is_admin=admin, secondary_staff_type=secondary)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
        return e

    mk("SC Admin", "sc_adm@x.com", "picker", admin=True)
    plain = mk("SC Picker", "sc_p@x.com", "picker")
    # A supervisor who also works as a picker, and so enters picker numbers.
    sup = mk("SC Super", "sc_su@x.com", "supervisor", secondary="picker")
    # A packer with no scored secondary role at all.
    pak = mk("SC Packer", "sc_pk@x.com", "packer")
    plain_id, sup_id, pak_id = plain.id, sup.id, pak.id

    # Identical picker numbers for all three, over the same 10 days.
    for eid in (plain_id, sup_id, pak_id):
        for back in range(10):
            d = TODAY - timedelta(days=back)
            if not KPIEntry.query.filter_by(emp_id=eid, entry_date=d).first():
                db.session.add(picker_entry(eid, d))
    db.session.commit()

    def stats(emp_id, staff_type):
        ents = KPIEntry.query.filter_by(emp_id=emp_id).all()
        return build_analytics(ents, staff_type, emp_id=emp_id)

    s_plain = stats(plain_id, "picker")
    s_sup = stats(sup_id, "supervisor")
    s_pak = stats(pak_id, "packer")

    # ---- 1. The supervisor is scored on the right formula ---------------
    check("1.1 Picker is scored as picker", s_plain.get("scored_as") == "picker")
    check("1.2 Supervisor with a picker secondary is scored as picker",
          s_sup.get("scored_as") == "picker")
    check("1.3 Supervisor's primary role is still reported",
          s_sup.get("primary_role") == "supervisor")
    check("1.4 Identical numbers give the supervisor the same score as the picker",
          abs(s_sup["eff_score"] - s_plain["eff_score"]) < 0.15)
    check("1.5 Supervisor is NOT falsely graded RE-TRAINING",
          s_sup["grade"] == s_plain["grade"])
    check("1.6 Supervisor is marked as scored", s_sup.get("is_scored") is True)

    # ---- 2. A role with no point system is reported, not graded ---------
    check("2.1 Packer with no scored secondary is flagged unscored",
          s_pak.get("is_scored") is False)
    check("2.2 Unscored role gets the NOT SCORED grade", s_pak["grade"] == "NOT SCORED")
    check("2.3 Unscored role is never called RE-TRAINING", s_pak["grade"] != "RE-TRAINING")
    check("2.4 Unscored role reports a zero score rather than a borrowed one",
          s_pak["eff_score"] == 0.0)
    check("2.5 Unscored role still reports its day count", s_pak.get("days") == 10)

    # ---- 3. The final score reconciles -----------------------------------
    for role in ("billing", "delivery"):
        if not MultitaskEntry.query.filter_by(
                emp_id=plain_id, entry_date=TODAY, secondary_type=role).first():
            db.session.add(MultitaskEntry(emp_id=plain_id, entry_date=TODAY,
                                          secondary_type=role, quantity=5,
                                          bills_done=4))
    db.session.commit()
    s = stats(plain_id, "picker")

    check("3.1 base_score is exposed", isinstance(s.get("base_score"), float))
    check("3.2 Two extra roles on one day is 20 points before averaging",
          s.get("multitask_day_points") == 20)
    check("3.3 The day count of multitask work is exposed",
          s.get("multitask_days") == 1)
    check("3.4 The bonus is the day points spread over the period",
          abs(s["multitask_bonus"] - round(20 / s["days"], 1)) < 0.11)
    check("3.5 base + bonus reconciles to the final score",
          abs((s["base_score"] + s["multitask_bonus"]) - s["eff_score"]) < 0.15)
    check("3.6 The bonus actually raised the score",
          s["eff_score"] > s["base_score"])
    check("3.7 Score never exceeds 100", s["eff_score"] <= 100.0)

client = csrf_client(app.test_client())
client.post("/login", data={"email": "sc_adm@x.com", "password": PW},
            follow_redirects=True)

# ---- 4. The admin can actually see it ----------------------------------
r = client.get("/staff/%d" % plain_id)
d = r.data
check("4.1 Staff profile renders", r.status_code == 200)
check("4.2 Profile shows the final score calculation", b"Final Score Calculation" in d)
check("4.3 Profile names the point system used", b"point system total" in d)
check("4.4 Profile shows the multitask bonus line", b"Multitask bonus" in d)
check("4.5 Profile explains how the bonus was spread",
      b"pts earned across" in d and b"spread over the" in d)

r = client.get("/staff/%d" % sup_id)
check("4.6 Supervisor profile explains which system scored them",
      b"point system because" in r.data)

r = client.get("/admin_dashboard")
d = r.data
check("4.7 Admin dashboard renders", r.status_code == 200)
check("4.8 Admin table surfaces the multitask bonus", b"multitask" in d.lower())
check("4.9 Unscored role shows NOT SCORED, not a failing grade",
      b"NOT SCORED" in d)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
