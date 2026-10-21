"""Regression suite for the IndianPharma IP system.

Run:  python tests_regression.py

Exercises the real app through Flask's test client against a local SQLite DB:
auth, every role's KPI/note entry, multitask across all 7 roles, the supervisor
console, the delivery assign -> trip -> stops lifecycle, admin panels, staff
profiles, exports, and a repo-wide emoji check.

Exit code 0 when everything passes, 1 otherwise.
"""
import os
import re
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date  # noqa: E402

from app import (  # noqa: E402
    app, db, Employee, KPIEntry, MultitaskEntry, DeliveryBillerNote,
    DeliveryAssignment, DeliveryStop, SupervisorBill,
)

app.config["TESTING"] = True
PASSED, FAILED = [], []
PW = "longpass123"


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False, secondary=None):
        emp = Employee.query.filter_by(email=email).first()
        if emp:
            return emp
        emp = Employee(name=name, email=email, staff_type=staff_type,
                       is_admin=admin, secondary_staff_type=secondary)
        emp.set_password(PW)
        db.session.add(emp)
        db.session.commit()
        return emp

    mk("Reg Admin",  "r_adm@x.com",  "picker", admin=True)
    mk("Reg Picker", "r_pick@x.com", "picker")
    mk("Reg Check",  "r_chk@x.com",  "checker")
    mk("Reg Purch",  "r_pur@x.com",  "purchaser")
    mk("Reg Driver", "r_drv@x.com",  "delivery")
    mk("Reg Biller", "r_bil@x.com",  "biller")
    mk("Reg Packer", "r_pak@x.com",  "packer")
    mk("Reg Super",  "r_sup@x.com",  "supervisor", secondary="picker")
    IDS = {e.email: e.id for e in Employee.query.all()}

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


# ---- 1. Authentication and access control -------------------------------
r = client.post("/login", data={"email": "r_pick@x.com", "password": "wrong"},
                follow_redirects=True)
check("1.1 Wrong password rejected", b"Invalid" in r.data or b"incorrect" in r.data.lower())
check("1.2 Correct password accepted", login("r_pick@x.com").status_code == 200)
check("1.3 Logout returns to login", b"assword" in client.get("/logout", follow_redirects=True).data)
check("1.4 Admin page blocked when logged out",
      b"assword" in client.get("/admin_dashboard", follow_redirects=True).data)
login("r_pick@x.com")
check("1.5 Admin page blocked for staff",
      b"Add New Employee" not in client.get("/admin_dashboard", follow_redirects=True).data)

# ---- 2. KPI entry per role ----------------------------------------------
login("r_pick@x.com")
client.post("/dashboard", data={
    "total_bills_received": "20", "sales_bills_open": "18", "picked": "140",
    "missed": "2", "cs_sales_open": "3", "packing_done": "9", "total_time": "8",
    "rack_organized": "5", "table_clean": "1", "sweep_done": "1"}, follow_redirects=True)
with app.app_context():
    e = KPIEntry.query.filter_by(emp_id=IDS["r_pick@x.com"], entry_date=date.today()).first()
check("2.1 Picker KPI saved", e is not None and e.picked == 140)

login("r_chk@x.com")
client.post("/dashboard", data={
    "bills_received": "22", "pending_bills_manual": "1", "sales_bills_open": "20",
    "cs_sales_open": "2", "checked": "180", "errors_found": "4", "total_time": "8",
    "rack_organized": "4", "table_clean": "1", "sweep_done": "1"}, follow_redirects=True)
with app.app_context():
    e = KPIEntry.query.filter_by(emp_id=IDS["r_chk@x.com"], entry_date=date.today()).first()
check("2.2 Checker KPI saved", e is not None and e.checked == 180)

login("r_pur@x.com")
client.post("/dashboard", data={
    "sales_bills_open": "14", "checked": "12", "picked": "10", "errors_found": "40",
    "cs_sales_open": "2", "packing_done": "5", "total_time": "8",
    "rack_organized": "3", "table_clean": "1", "sweep_done": "1"}, follow_redirects=True)
with app.app_context():
    e = KPIEntry.query.filter_by(emp_id=IDS["r_pur@x.com"], entry_date=date.today()).first()
check("2.3 Purchaser KPI saved", e is not None)

# ---- 3. Daily notes for delivery / biller / packer ----------------------
for email, text in (("r_drv@x.com", "drove 12 stops"),
                    ("r_bil@x.com", "assigned 30 bills"),
                    ("r_pak@x.com", "packed 90 items")):
    login(email)
    client.post("/dashboard", data={
        "delivery_note": text, "delivery_qty": "12", "delivery_issues": "0",
        "table_clean": "1", "sweep_done": "1"}, follow_redirects=True)
with app.app_context():
    check("3.1 Daily notes saved for all three roles", DeliveryBillerNote.query.count() >= 3)

# ---- 4. Multitask across every role, with field mapping ----------------
MULTITASK = {
    "picker":    {"mt_p_total_bills": "2", "mt_p_sbo": "10", "mt_p_picked": "50",
                  "mt_p_missed": "3", "mt_p_cso": "1", "mt_p_packing": "5"},
    "checker":   {"mt_c_bills_received": "8", "mt_c_pending": "1", "mt_c_sbo": "4",
                  "mt_c_cso": "2", "mt_c_checked": "60", "mt_c_errors": "7"},
    "purchaser": {"mt_pu_sbo": "15", "mt_pu_checked": "12", "mt_pu_picked": "9",
                  "mt_pu_items": "30", "mt_pu_cso": "3", "mt_pu_packing": "6"},
    "billing":   {"mt_b_bills": "11", "mt_b_items": "140", "mt_b_quantity": "11"},
    "delivery":  {"mt_s_quantity__delivery": "8"},
    "packing":   {"mt_s_quantity__packing": "4"},
    "other":     {"mt_o_breakage": "5", "mt_o_expire": "3", "mt_o_processed": "7",
                  "mt_o_recv_qty": "2", "mt_o_exp_return": "1"},
}
login("r_sup@x.com")  # supervisor primary collides with none of the seven
for role, fields in MULTITASK.items():
    payload = {"multitask_only": "1", "multitask_roles": [role],
               "multitask_note": "mt " + role}
    payload.update(fields)
    client.post("/supervisor", data=payload, follow_redirects=True)
with app.app_context():
    mts = {m.secondary_type: m
           for m in MultitaskEntry.query.filter_by(emp_id=IDS["r_sup@x.com"]).all()}
check("4.1 All seven multitask roles saved", len(mts) == 7)
check("4.2 Picker fields mapped",
      bool(mts.get("picker")) and mts["picker"].picked == 50 and mts["picker"].total_bills_received == 2)
check("4.3 Checker fields mapped",
      bool(mts.get("checker")) and mts["checker"].checked == 60 and mts["checker"].bills_received == 8)
check("4.4 Purchaser fields mapped",
      bool(mts.get("purchaser")) and mts["purchaser"].sales_bills_open == 15
      and mts["purchaser"].picked == 9 and mts["purchaser"].packing_done == 6)
check("4.5 Billing fields mapped",
      bool(mts.get("billing")) and mts["billing"].bills_done == 11 and mts["billing"].items_count == 140)
check("4.6 Delivery quantity mapped", bool(mts.get("delivery")) and mts["delivery"].quantity == 8)
check("4.7 Packing quantity mapped", bool(mts.get("packing")) and mts["packing"].quantity == 4)
check("4.8 Other fields mapped",
      bool(mts.get("other")) and mts["other"].breakage_received == 5
      and mts["other"].expire_return_qty == 1)
check("4.9 Purchaser summary lists every parameter",
      bool(mts.get("purchaser")) and all(k in mts["purchaser"].summary for k in (
          "PO Bills Rcvd", "PO Checked", "PO Entry", "Items", "CS Open", "CS Rcvd")))
check("4.10 Checker summary lists every parameter",
      bool(mts.get("checker")) and all(k in mts["checker"].summary for k in (
          "Bills Rcvd", "Pending", "SB Checked", "Items Checked", "Urgent")))

# ---- 5. Supervisor console ---------------------------------------------
login("r_sup@x.com")
check("5.1 Supervisor lands on the console",
      b"Supervisor Console" in client.get("/dashboard", follow_redirects=True).data)
client.post("/supervisor", data={
    "staff_id": str(IDS["r_pick@x.com"]), "entry_date": date.today().isoformat(),
    "super_bills": "25", "note": "counted at till"}, follow_redirects=True)
with app.app_context():
    sb = SupervisorBill.query.filter_by(staff_id=IDS["r_pick@x.com"]).first()
check("5.2 Supervisor bill total saved", sb is not None and sb.super_bills == 25)
r = client.post("/switch_role", data={"role": "picker"}, follow_redirects=True)
check("5.3 Switch to held secondary role works", b"Supervisor Console" not in r.data)
client.post("/switch_role", data={"role": "supervisor"}, follow_redirects=True)
r = client.post("/switch_role", data={"role": "checker"}, follow_redirects=True)
check("5.4 Switch to a role not held is refused", b"Invalid role switch" in r.data)

# ---- 6. Delivery lifecycle ---------------------------------------------
login("r_bil@x.com")
client.post("/delivery/assign", data={
    "delivery_emp_id": str(IDS["r_drv@x.com"]), "packet_type": "Box",
    "store_name[]": ["Reg Store A", "Reg Store B"],
    "store_area[]": ["Cuttack", "Cuttack"],
    "store_packages[]": ["2", "3"],
    "store_lat[]": ["20.47", "20.48"], "store_lng[]": ["85.89", "85.90"],
    "store_packet_type[]": ["Box", "Box"]}, follow_redirects=True)
with app.app_context():
    a = DeliveryAssignment.query.order_by(DeliveryAssignment.id.desc()).first()
    aid = a.id if a else 0
    nstops = DeliveryStop.query.filter_by(assignment_id=aid).count() if aid else 0
    has_coords = bool(a and a.destination_lat is not None)
check("6.1 Assignment created", aid != 0)
check("6.2 Both stops created", nstops == 2)
check("6.3 Assignment carries coordinates", has_coords)

client.post("/delivery/assign", data={
    "delivery_emp_id": str(IDS["r_drv@x.com"]), "packet_type": "Box",
    "store_name[]": ["Zzz Ungeocodable Store"], "store_area[]": [""],
    "store_packages[]": ["1"], "store_lat[]": [""], "store_lng[]": [""],
    "store_packet_type[]": ["Box"]}, follow_redirects=True)
with app.app_context():
    a2 = DeliveryAssignment.query.order_by(DeliveryAssignment.id.desc()).first()
check("6.4 Geocode failure still yields fallback coordinates",
      bool(a2) and a2.destination_lat is not None)

login("r_drv@x.com")
check("6.5 Rider sees a map container", b"dlmap-" in client.get("/dashboard").data)
r = client.post("/delivery/start_trip/%d" % aid, json={"lat": 20.4625, "lng": 85.8830})
check("6.6 Trip start returns JSON", r.content_type.startswith("application/json"))
with app.app_context():
    a = db.session.get(DeliveryAssignment, aid)
check("6.7 Trip start moves assignment to in_transit", bool(a) and a.status == "in_transit")

login("r_adm@x.com")
r = client.get("/admin/delivery")
check("6.8 Admin delivery page renders a map", r.status_code == 200 and b"journey-map" in r.data)
check("6.9 Stop names reach the admin map", b"Reg Store A" in r.data)
check("6.10 Admin day picker accepts a date",
      client.get("/admin/delivery?day=%s" % date.today().isoformat()).status_code == 200)
check("6.11 Admin day picker survives junk input",
      client.get("/admin/delivery?day=not-a-date").status_code == 200)
check("6.12 Delivery leaderboard renders",
      client.get("/delivery/leaderboard").status_code == 200)

# ---- 7. Admin panels ----------------------------------------------------
login("r_adm@x.com")
r = client.get("/admin_dashboard")
d = r.data
check("7.1 Admin dashboard renders", r.status_code == 200)
check("7.2 Multitask panel present", b"Multitask (Role 2) Reports" in d)
check("7.3 Multitask numbers shown", b"PO Bills Rcvd" in d or b"Items Checked" in d)
check("7.4 Supervisor offered as a plain role", b'value="supervisor">Supervisor<' in d)
check("7.5 No leftover '(hidden)' label", b"(hidden)" not in d)
check("7.6 Every staff type listed",
      all(n in d for n in (b"Reg Driver", b"Reg Super", b"Reg Packer")))
check("7.7 Daily notes surfaced", b"drove 12 stops" in d or b"packed 90 items" in d)

client.post("/admin/add_user", data={
    "name": "Fresh Sup", "email": "fresh@x.com", "password": PW,
    "staff_type": "supervisor"}, follow_redirects=True)
with app.app_context():
    fresh = Employee.query.filter_by(email="fresh@x.com").first()
    fresh_id = fresh.id if fresh else 0
    fresh_ok = bool(fresh) and fresh.staff_type == "supervisor"
check("7.8 Admin can add a supervisor", fresh_ok)

client.post("/admin/add_user", data={
    "name": "Short PW", "email": "short@x.com", "password": "abc",
    "staff_type": "picker"}, follow_redirects=True)
with app.app_context():
    check("7.9 Password under 8 chars refused",
          Employee.query.filter_by(email="short@x.com").first() is None)

if fresh_id:
    client.post("/admin/edit_user/%d" % fresh_id, data={
        "name": "Fresh Sup Edited", "email": "fresh@x.com",
        "staff_type": "supervisor", "secondary_staff_type": "delivery"},
        follow_redirects=True)
    with app.app_context():
        f2 = db.session.get(Employee, fresh_id)
        check("7.10 Admin can edit a supervisor", bool(f2) and f2.name == "Fresh Sup Edited")
        check("7.11 Secondary role persists", bool(f2) and f2.secondary_staff_type == "delivery")

# ---- 8. Staff profile ---------------------------------------------------
r = client.get("/staff/%d" % IDS["r_sup@x.com"])
d = r.data
check("8.1 Staff profile renders", r.status_code == 200)
check("8.2 Multitask section present", b"Multitask" in d)
check("8.3 Role-specific labels rendered", b"PO Bills Rcvd:" in d and b"CS Rcvd:" in d)
check("8.4 Generic Qty column header gone", b">Qty</th>" not in d)
check("8.5 Delivery note on that staff's profile",
      b"drove 12 stops" in client.get("/staff/%d" % IDS["r_drv@x.com"]).data)

login("r_pick@x.com")
r = client.get("/staff/%d" % IDS["r_chk@x.com"], follow_redirects=True)
check("8.6 Staff cannot read another staff profile",
      b"Reg Check" not in r.data or r.status_code != 200)

# ---- 9. Exports ---------------------------------------------------------
login("r_adm@x.com")
r = client.get("/admin/export_csv")
check("9.1 CSV export returns data", r.status_code == 200 and len(r.data) > 50)
r = client.get("/admin/export_full")
check("9.2 JSON export returns data", r.status_code == 200)
check("9.3 JSON export leaks no password hashes",
      not any(t in r.data for t in (b"password_hash", b"pbkdf2", b"scrypt")))

# ---- 10. No emoji anywhere in the source -------------------------------
EMOJI = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️]")
offenders = []
for root, _dirs, files in os.walk(REPO):
    if ".git" in root:
        continue
    for fn in files:
        if not fn.endswith((".py", ".html")) or fn == os.path.basename(__file__):
            continue
        path = os.path.join(root, fn)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for m in EMOJI.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append("%s:%d %r" % (os.path.relpath(path, REPO), line, m.group()))
check("10.1 No emoji in .py or .html sources", not offenders)
for o in offenders[:15]:
    print("      emoji: " + o)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
