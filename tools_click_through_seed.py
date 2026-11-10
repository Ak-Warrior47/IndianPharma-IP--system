"""Local test accounts and one bill for tools_click_through.js (SQLite only; never the live database)."""
import os, sys, logging
logging.disable(logging.CRITICAL)
os.environ.pop("DATABASE_URL", None)     # never the live database
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A
from datetime import date
from app import db, Employee, PickClaim, PickBill
with A.app.app_context():
    def mk(email, name, st, admin=False):
        e = Employee.query.filter_by(email=email).first()
        if not e:
            e = Employee(name=name, email=email, staff_type=st, is_admin=admin)
            db.session.add(e)
        e.set_password("longpass123"); e.staff_type = st; e.is_admin = admin
        e.session_key = None; e.session_keys = None
        db.session.commit()
        return e
    mk("adm@x.com", "Owner", "picker", True)
    asg = mk("asg@x.com", "Ravi", "pick_assigner")
    mk("lisa@x.com", "Lisa", "picker"); mk("amit@x.com", "Amit", "picker"); mk("pur@x.com", "Deepak", "purchaser"); mk("chk@x.com", "Smriti", "checker")
    A.PickDay.query.filter_by(day=date.today()).delete()
    if db.engine.dialect.name != "sqlite":
        raise SystemExit("Refusing to run: this is not the local SQLite test database.")
    for M in (A.PickMissing, A.PickEvent, PickClaim, PickBill):     # a clean floor each run
        M.query.delete()
    db.session.commit()
    A.add_pick_bill("66201", 6, asg.id)
print("seeded")
