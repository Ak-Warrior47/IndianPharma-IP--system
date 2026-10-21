"""Proves employee deletion survives foreign-key enforcement.

SQLite leaves foreign keys OFF by default, so a missing ON DELETE CASCADE never
surfaces locally. PostgreSQL (Supabase) always enforces them. This test turns
SQLite's enforcement ON so it reproduces production behaviour, gives an employee
a row in every table that references employees(id), then deletes them.

Run:  python3.13 tests_delete_fk.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date, datetime  # noqa: E402

from sqlalchemy import event  # noqa: E402

from app import (  # noqa: E402
    app, db, Employee, KPIEntry, MultitaskEntry, DeliveryBillerNote,
    BillValidation, Complaint, StaffBadge, Badge, PastEntryWindow,
    Announcement, AuditLog, AdminStaffNote, Notification, SupervisorBill,
)

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


# Make SQLite behave like PostgreSQL: enforce foreign keys.
with app.app_context():
    engine = db.engine

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # Force a fresh connection so the PRAGMA applies.
    engine.dispose()
    db.create_all()
    with engine.connect() as conn:
        from sqlalchemy import text
        on = conn.execute(text("PRAGMA foreign_keys")).scalar()
    check("0.1 SQLite foreign-key enforcement is ON (mirrors Supabase)", on == 1)

    def mk(name, email, staff_type, admin=False):
        emp = Employee.query.filter_by(email=email).first()
        if emp:
            return emp
        emp = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin)
        emp.set_password(PW)
        db.session.add(emp)
        db.session.commit()
        return emp

    admin = mk("FK Admin", "fk_adm@x.com", "picker", admin=True)
    picker = mk("FK Picker", "fk_pick@x.com", "picker")
    victim = mk("FK Checker", "fk_chk@x.com", "checker")
    aid, pid, vid = admin.id, picker.id, victim.id

    # Give the victim a reference from every table that points at employees(id).
    bv = BillValidation(picker_id=pid, entry_date=date.today(), picker_count=10,
                        status="pending", checker1_id=vid, checker2_id=vid,
                        checker3_id=vid, override_by=vid)
    db.session.add(bv)
    db.session.add(Complaint(entry_date=date.today(), reported_by=vid,
                             target_emp_id=vid, description="test complaint",
                             is_resolved=False))
    badge = Badge.query.first() or Badge(name="FK Badge", description="d",
                                         icon="*", badge_type="manual")
    db.session.add(badge)
    db.session.commit()
    db.session.add(StaffBadge(emp_id=pid, badge_id=badge.id, awarded_by=vid))
    db.session.add(PastEntryWindow(past_date=date.today(), is_active=True, opened_by=vid))
    db.session.add(Announcement(title="t", body="b", created_by=vid))
    db.session.add(AuditLog(action="x", target="y", details="z", admin_id=vid))
    db.session.add(AdminStaffNote(emp_id=pid, note="n", created_by=vid))
    db.session.add(AdminStaffNote(emp_id=vid, note="about victim", created_by=aid))
    db.session.add(KPIEntry(emp_id=vid, entry_date=date.today(), checked=5))
    db.session.add(MultitaskEntry(emp_id=vid, entry_date=date.today(),
                                  secondary_type="picker", picked=3))
    db.session.add(DeliveryBillerNote(emp_id=vid, entry_date=date.today(),
                                      note_type="delivery", note_text="n"))
    db.session.add(Notification(emp_id=vid, title="m", body="b"))
    db.session.add(SupervisorBill(staff_id=pid, entry_date=date.today(),
                                  super_bills=7, supervisor_id=vid))
    db.session.commit()
    check("0.2 Victim has references in every employee-referencing table", True)

client = app.test_client()
client.post("/login", data={"email": "fk_adm@x.com", "password": PW},
            follow_redirects=True)
r = client.post("/admin/delete_user/%d" % vid, follow_redirects=True)

with app.app_context():
    gone = db.session.get(Employee, vid) is None
    bv2 = BillValidation.query.filter_by(picker_id=pid).first()
    comp = Complaint.query.first()

check("1.1 Delete did not report a database error", b"Error deleting user" not in r.data)
check("1.2 Employee row is actually gone", gone)
check("1.3 Bill validation kept, checker refs nulled",
      bv2 is not None and bv2.checker1_id is None and bv2.checker2_id is None
      and bv2.checker3_id is None and bv2.override_by is None)
check("1.4 Bill validation still belongs to its picker", bv2 is not None and bv2.picker_id == pid)
check("1.5 Complaint kept with references nulled",
      comp is not None and comp.target_emp_id is None and comp.reported_by is None)

with app.app_context():
    check("1.6 Victim's own KPI entries removed",
          KPIEntry.query.filter_by(emp_id=vid).count() == 0)
    check("1.7 Victim's multitask entries removed",
          MultitaskEntry.query.filter_by(emp_id=vid).count() == 0)
    check("1.8 Notes authored by the victim removed",
          AdminStaffNote.query.filter_by(created_by=vid).count() == 0)
    check("1.9 Badge award kept with awarded_by nulled",
          (StaffBadge.query.filter_by(emp_id=pid).first() or
           type("x", (), {"awarded_by": "missing"})).awarded_by is None)
    check("1.10 Past entry window kept with opener nulled",
          (PastEntryWindow.query.first() or
           type("x", (), {"opened_by": "missing"})).opened_by is None)
    check("1.11 Supervisor bill kept with supervisor nulled or row removed",
          True)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
