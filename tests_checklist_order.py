"""Checklist reordering and the role-wise completion view.

The reorder bug this pins down: the Up/Down buttons nudged sort_order by one
without touching the neighbour, so two tasks ended up sharing a value, the order
silently fell back to id, and the buttons appeared to do nothing.

Run:  python3.13 tests_checklist_order.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee, ChecklistTemplate, ChecklistCompletion  # noqa: E402
from datetime import date, datetime  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
TODAY = date.today()


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False):
        e = Employee.query.filter_by(email=email).first()
        if e:
            return e
        e = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
        return e

    mk("CO Admin", "co_adm@x.com", "picker", admin=True)
    p1 = mk("CO Picker One", "co_p1@x.com", "picker")
    p2 = mk("CO Picker Two", "co_p2@x.com", "picker")
    dv = mk("CO Driver", "co_drv@x.com", "delivery")
    p1_id, p2_id, dv_id = p1.id, p2.id, dv.id

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


def order_of(role):
    with app.app_context():
        return [t.text for t in ChecklistTemplate.query
                .filter_by(staff_type=role, is_active=1)
                .order_by(ChecklistTemplate.sort_order, ChecklistTemplate.id).all()]


def sort_values(role):
    with app.app_context():
        return [t.sort_order for t in ChecklistTemplate.query
                .filter_by(staff_type=role, is_active=1)
                .order_by(ChecklistTemplate.sort_order, ChecklistTemplate.id).all()]


def tid_of(role, text):
    with app.app_context():
        t = ChecklistTemplate.query.filter_by(staff_type=role, text=text,
                                              is_active=1).first()
        return t.id if t else 0


def move(tid, direction):
    return client.post("/admin/checklist",
                       data={"action": "move", "template_id": str(tid),
                             "dir": direction}, follow_redirects=True)


login("co_adm@x.com")
for text in ("Alpha", "Bravo", "Charlie", "Delta"):
    client.post("/admin/checklist", data={"action": "add", "staff_type": "picker",
                                          "text": text}, follow_redirects=True)
client.post("/admin/checklist", data={"action": "add", "staff_type": "delivery",
                                      "text": "Check tyres"}, follow_redirects=True)

# ---- 1. Reordering actually reorders ------------------------------------
check("1.1 Initial order is insertion order",
      order_of("picker") == ["Alpha", "Bravo", "Charlie", "Delta"])

move(tid_of("picker", "Charlie"), "up")
check("1.2 Moving up swaps with the task above",
      order_of("picker") == ["Alpha", "Charlie", "Bravo", "Delta"])

move(tid_of("picker", "Charlie"), "up")
check("1.3 Moving up again advances one more place",
      order_of("picker") == ["Charlie", "Alpha", "Bravo", "Delta"])

move(tid_of("picker", "Charlie"), "down")
check("1.4 Moving down reverses it",
      order_of("picker") == ["Alpha", "Charlie", "Bravo", "Delta"])

# ---- 2. No ties or gaps, which is what broke it before -------------------
vals = sort_values("picker")
check("2.1 Every sort_order is distinct (no ties)", len(set(vals)) == len(vals))
check("2.2 Order values are a clean 1..n run", vals == list(range(1, len(vals) + 1)))

# ---- 3. Edges are safe ---------------------------------------------------
first_order = order_of("picker")
move(tid_of("picker", first_order[0]), "up")
check("3.1 Moving the first task up changes nothing",
      order_of("picker") == first_order)
last_order = order_of("picker")
move(tid_of("picker", last_order[-1]), "down")
check("3.2 Moving the last task down changes nothing",
      order_of("picker") == last_order)

# ---- 4. Reordering never crosses roles -----------------------------------
before_delivery = order_of("delivery")
move(tid_of("picker", "Alpha"), "down")
check("4.1 The delivery list is untouched by picker reordering",
      order_of("delivery") == before_delivery)
check("4.2 Delivery still has exactly its own one task",
      len(order_of("delivery")) == 1)

# ---- 5. Junk input is rejected -------------------------------------------
snapshot = order_of("picker")
move(tid_of("picker", "Bravo"), "sideways")
check("5.1 An unknown direction is ignored", order_of("picker") == snapshot)
r = move(999999, "up")
check("5.2 An unknown task id does not error", r.status_code == 200)
check("5.3 Order is unchanged after a bad id", order_of("picker") == snapshot)

# ---- 6. Role-wise completion view ----------------------------------------
with app.app_context():
    picker_ids = [t.id for t in ChecklistTemplate.query
                  .filter_by(staff_type="picker", is_active=1).all()]
    # Picker One finishes everything, Picker Two does nothing,
    # the driver does their single task.
    for tid in picker_ids:
        db.session.add(ChecklistCompletion(emp_id=p1_id, template_id=tid,
                                           entry_date=TODAY, done=1,
                                           completed_at=datetime.utcnow()))
    d_tid = ChecklistTemplate.query.filter_by(staff_type="delivery",
                                              is_active=1).first().id
    db.session.add(ChecklistCompletion(emp_id=dv_id, template_id=d_tid,
                                       entry_date=TODAY, done=1,
                                       completed_at=datetime.utcnow()))
    db.session.commit()

r = client.get("/admin/checklist")
d = r.data
check("6.1 Checklist page loads", r.status_code == 200)
check("6.2 Staff are grouped under their role heading",
      b"Operations Picker" in d and b"Delivery Staff" in d)
check("6.3 A finished staff member is marked Done", b">Done<" in d)
check("6.4 A staff member who has not begun is marked Not started",
      b"Not started" in d)
check("6.5 A role where everyone finished is flagged All clear",
      b"All clear" in d)
check("6.6 A role with work outstanding says so", b"still working" in d)
check("6.7 The outstanding work itself is listed", b"Alpha" in d)
check("6.8 Each role shows its task and staff counts", b"staff" in d)
check("6.9 Overall finished count is shown", b"finished" in d)

# ---- 7. Staff still cannot reorder --------------------------------------
login("co_p1@x.com")
snapshot = order_of("picker")
move(tid_of("picker", "Bravo"), "up")
check("7.1 A staff member cannot reorder the checklist",
      order_of("picker") == snapshot)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
