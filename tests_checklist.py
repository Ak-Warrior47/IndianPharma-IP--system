"""Checklist feature: admin-defined per-role templates, staff completion.

Covers the logic and the loopholes: a staff member must not be able to tick an
item belonging to another role, an archived item, or a non-existent one; and
completion must be scoped to the right person and the right day.

Run:  python3.13 tests_checklist.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date, timedelta  # noqa: E402

from app import (  # noqa: E402
    app, db, Employee, ChecklistTemplate, ChecklistCompletion,
    checklist_for, checklist_pct,
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
        e = Employee.query.filter_by(email=email).first()
        if e:
            return e
        e = Employee(name=name, email=email, staff_type=staff_type, is_admin=admin)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
        return e

    mk("CL Admin", "cl_adm@x.com", "picker", admin=True)
    p1 = mk("CL Picker One", "cl_p1@x.com", "picker")
    p2 = mk("CL Picker Two", "cl_p2@x.com", "picker")
    dv = mk("CL Driver", "cl_drv@x.com", "delivery")
    p1_id, p2_id, dv_id = p1.id, p2.id, dv.id

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


# ---- 1. Admin creates templates -----------------------------------------
login("cl_adm@x.com")
r = client.get("/admin/checklist")
check("1.1 Checklist manager loads", r.status_code == 200)
check("1.2 Empty state explains itself when nothing is defined",
      b"No checklists are live yet" in r.data or b"No active tasks" in r.data)

for text in ("Clear the pick face", "Sign the bin sheet", "Wipe the bench"):
    client.post("/admin/checklist", data={"action": "add", "staff_type": "picker",
                                          "text": text}, follow_redirects=True)
client.post("/admin/checklist", data={"action": "add", "staff_type": "delivery",
                                      "text": "Check tyre pressure"},
            follow_redirects=True)
with app.app_context():
    picker_t = ChecklistTemplate.query.filter_by(staff_type="picker", is_active=1).all()
    driver_t = ChecklistTemplate.query.filter_by(staff_type="delivery", is_active=1).all()
    picker_ids = [t.id for t in picker_t]
    driver_id = driver_t[0].id if driver_t else 0
check("1.3 Three picker tasks created", len(picker_t) == 3)
check("1.4 One delivery task created", len(driver_t) == 1)
check("1.5 Sort order assigned incrementally",
      sorted(t.sort_order for t in picker_t) == [1, 2, 3])

r = client.post("/admin/checklist", data={"action": "add", "staff_type": "nonsense",
                                          "text": "hack"}, follow_redirects=True)
with app.app_context():
    check("1.6 Invalid staff type refused",
          ChecklistTemplate.query.filter_by(staff_type="nonsense").count() == 0)
r = client.post("/admin/checklist", data={"action": "add", "staff_type": "picker",
                                          "text": "   "}, follow_redirects=True)
with app.app_context():
    check("1.7 Blank task text refused",
          ChecklistTemplate.query.filter_by(staff_type="picker", is_active=1).count() == 3)

# ---- 2. Staff see their own role's list ---------------------------------
with app.app_context():
    check("2.1 Picker sees three items", len(checklist_for(p1_id, "picker")) == 3)
    check("2.2 Driver sees one item", len(checklist_for(dv_id, "delivery")) == 1)
    check("2.3 Role with no list gets an empty list",
          checklist_for(p1_id, "checker") == [])
    check("2.4 Percentage is None when no list is defined",
          checklist_pct(p1_id, "checker") is None)
    check("2.5 Percentage starts at 0", checklist_pct(p1_id, "picker") == 0.0)

login("cl_p1@x.com")
r = client.get("/dashboard")
check("2.6 Checklist card renders on the staff dashboard", b"Today's Checklist" in r.data)
check("2.7 Task text visible", b"Clear the pick face" in r.data)
check("2.8 Another role's task is not shown", b"Check tyre pressure" not in r.data)

# ---- 3. Ticking works and is scoped ------------------------------------
r = client.post("/checklist/toggle", json={"template_id": picker_ids[0], "done": True})
body = r.get_json() or {}
check("3.1 Tick accepted", r.status_code == 200 and body.get("ok") is True)
check("3.2 Response reports progress",
      body.get("completed") == 1 and body.get("total") == 3)
with app.app_context():
    check("3.3 Completion stored for the right person and day",
          ChecklistCompletion.query.filter_by(
              emp_id=p1_id, template_id=picker_ids[0], entry_date=TODAY).first() is not None)
    check("3.4 Percentage reflects one of three", checklist_pct(p1_id, "picker") == 33.3)
    row = ChecklistCompletion.query.filter_by(emp_id=p1_id,
                                              template_id=picker_ids[0]).first()
    check("3.5 Completion timestamp recorded", row is not None and row.completed_at is not None)

r = client.post("/checklist/toggle", json={"template_id": picker_ids[0], "done": False})
check("3.6 Un-ticking works", (r.get_json() or {}).get("done") is False)
with app.app_context():
    check("3.7 Un-tick keeps the row for audit, sets done=0",
          (ChecklistCompletion.query.filter_by(
              emp_id=p1_id, template_id=picker_ids[0]).first() or
           type("x", (), {"done": 9})).done == 0)
    check("3.8 Un-tick does not leak into the other picker's record",
          checklist_pct(p2_id, "picker") == 0.0)

# ---- 4. Loopholes must be closed ---------------------------------------
r = client.post("/checklist/toggle", json={"template_id": driver_id, "done": True})
check("4.1 Picker cannot tick a delivery task", r.status_code == 403)
with app.app_context():
    check("4.2 No cross-role completion written",
          ChecklistCompletion.query.filter_by(emp_id=p1_id,
                                              template_id=driver_id).count() == 0)

r = client.post("/checklist/toggle", json={"template_id": 999999, "done": True})
check("4.3 Unknown template refused", r.status_code == 403)
r = client.post("/checklist/toggle", json={})
check("4.4 Missing template id refused", r.status_code == 403)

login("cl_adm@x.com")
client.post("/admin/checklist", data={"action": "archive",
                                      "template_id": picker_ids[2]},
            follow_redirects=True)
login("cl_p1@x.com")
r = client.post("/checklist/toggle", json={"template_id": picker_ids[2], "done": True})
check("4.5 Archived task cannot be ticked", r.status_code == 403)
with app.app_context():
    check("4.6 Archived task drops off the live list",
          len(checklist_for(p1_id, "picker")) == 2)

client.get("/logout")
r = client.post("/checklist/toggle", json={"template_id": picker_ids[0], "done": True})
check("4.7 Logged-out toggle is refused", r.status_code in (301, 302, 401, 403))

login("cl_p1@x.com")
r = client.get("/admin/checklist", follow_redirects=True)
check("4.8 Staff cannot open the checklist manager", b"Add a Task" not in r.data)

# ---- 5. History is per day ---------------------------------------------
with app.app_context():
    db.session.add(ChecklistCompletion(emp_id=p1_id, template_id=picker_ids[1],
                                       entry_date=TODAY - timedelta(days=1), done=1))
    db.session.commit()
    check("5.1 Yesterday's tick does not count toward today",
          checklist_pct(p1_id, "picker") == 0.0)
    check("5.2 Yesterday's record is still retrievable",
          len([t for t, d in checklist_for(p1_id, "picker",
                                           TODAY - timedelta(days=1)) if d]) == 1)

# ---- 6. Admin sees completion -----------------------------------------
login("cl_p1@x.com")
client.post("/checklist/toggle", json={"template_id": picker_ids[0], "done": True})
login("cl_adm@x.com")
r = client.get("/admin/checklist")
d = r.data
check("6.1 Admin sees the staff completion table", b"Today's Completion" in d)
check("6.2 Staff member listed", b"CL Picker One" in d)
check("6.3 Pending item surfaced for the admin", b"Sign the bin sheet" in d)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
