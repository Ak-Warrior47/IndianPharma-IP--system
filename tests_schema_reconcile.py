"""Proves reconcile_schema() repairs a database whose tables lag the models.

This reproduces the production symptom: a column exists on the model but not in
the live table, so every query touching it raises, the caller swallows the error
into an empty list, and the admin panel renders blank with nothing on screen to
explain why.

The test drops real columns from a built database, confirms the breakage, runs
reconcile_schema(), and confirms the repair.

Run:  python3.13 tests_schema_reconcile.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date  # noqa: E402

from sqlalchemy import inspect as sa_inspect  # noqa: E402

from app import (  # noqa: E402
    app, db, Employee, MultitaskEntry, reconcile_schema,
)

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


def live_columns(table):
    return {c["name"] for c in sa_inspect(db.engine).get_columns(table)}


# Columns we will amputate from multitask_entries. These are exactly the kind
# that were added to the model over time and had to be remembered in the
# hand-written migration lists.
VICTIMS = ["bills_done", "items_count", "breakage_received", "expire_return_qty"]

with app.app_context():
    db.create_all()

    emp = Employee.query.filter_by(email="sr_sup@x.com").first()
    if not emp:
        emp = Employee(name="SR Super", email="sr_sup@x.com", staff_type="supervisor")
        emp.set_password(PW)
        db.session.add(emp)
        db.session.commit()
    admin = Employee.query.filter_by(email="sr_adm@x.com").first()
    if not admin:
        admin = Employee(name="SR Admin", email="sr_adm@x.com",
                         staff_type="picker", is_admin=True)
        admin.set_password(PW)
        db.session.add(admin)
        db.session.commit()
    emp_id = emp.id

    model_cols = {c.name for c in MultitaskEntry.__table__.columns}
    check("0.1 All model columns present before damage",
          VICTIMS and set(VICTIMS).issubset(live_columns("multitask_entries")))

    # Amputate the victim columns the way an older deployment's table would look.
    # DROP COLUMN (SQLite 3.35+) is used rather than rebuilding via CREATE TABLE AS
    # SELECT, because the latter would also destroy the primary key and
    # autoincrement, which real schema drift never does.
    for v in VICTIMS:
        db.session.execute(db.text(
            "ALTER TABLE multitask_entries DROP COLUMN %s" % v))
    db.session.commit()
    db.session.expire_all()

    after = live_columns("multitask_entries")
    check("1.1 Victim columns really are gone",
          not any(v in after for v in VICTIMS))

    # Confirm the production symptom: querying the model now raises.
    broke = False
    try:
        MultitaskEntry.query.filter(MultitaskEntry.entry_date >= date(2000, 1, 1)).all()
    except Exception:
        broke = True
    db.session.rollback()
    check("1.2 Querying the model raises while the table lags (the real symptom)", broke)

    # The admin dashboard swallows that error, so the panel goes blank silently.
    client = app.test_client()
    client.post("/login", data={"email": "sr_adm@x.com", "password": PW},
                follow_redirects=True)
    r = client.get("/admin_dashboard")
    check("1.3 Admin dashboard still returns 200 (error is swallowed, panel blank)",
          r.status_code == 200)

    # Now repair.
    added = reconcile_schema()
    repaired = live_columns("multitask_entries")
    check("2.1 reconcile_schema reported the missing columns",
          all(("multitask_entries." + v) in added for v in VICTIMS))
    check("2.2 Every victim column is back", set(VICTIMS).issubset(repaired))
    check("2.3 No model column is missing any more",
          model_cols.issubset(repaired))

    # Querying works again, and the model is usable end to end.
    db.session.expire_all()
    works = True
    try:
        MultitaskEntry.query.filter(MultitaskEntry.entry_date >= date(2000, 1, 1)).all()
    except Exception:
        works = False
    check("2.4 Querying the model works again", works)

    db.session.add(MultitaskEntry(emp_id=emp_id, entry_date=date.today(),
                                  secondary_type="billing", bills_done=9,
                                  items_count=120, quantity=9))
    db.session.commit()
    row = MultitaskEntry.query.filter_by(emp_id=emp_id,
                                         secondary_type="billing").first()
    check("2.5 Restored columns accept and return data",
          row is not None and row.bills_done == 9 and row.items_count == 120)
    check("2.6 Summary renders from the restored columns",
          row is not None and "Bills Done: 9" in row.summary)

    # Idempotent: a second run must add nothing and must not throw.
    again = reconcile_schema()
    check("3.1 Second run is a no-op (idempotent)", again == [])

with app.test_client() as client:
    client.post("/login", data={"email": "sr_adm@x.com", "password": PW},
                follow_redirects=True)
    r = client.get("/admin_dashboard")
    check("4.1 Admin dashboard renders after repair", r.status_code == 200)
    check("4.2 Multitask panel shows the restored entry",
          b"Bills Done: 9" in r.data)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
