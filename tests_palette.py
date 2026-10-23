"""Command palette: the /api/palette endpoint and its permission scoping.

The palette is a data endpoint, so the thing worth testing is not that it
returns results but that it never returns MORE than the caller may see. A staff
member must not be able to enumerate colleagues or discover admin screens
through it.

Run:  python3.13 tests_palette.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name)


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

    mk("PK Admin",  "pk_adm@x.com", "picker", admin=True)
    pick = mk("PK Picker", "pk_p@x.com", "picker")
    mk("PK Secret", "pk_s@x.com", "checker")
    drv = mk("PK Driver", "pk_d@x.com", "delivery")
    sup = mk("PK Super",  "pk_su@x.com", "supervisor")
    pick_id = pick.id

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": PW},
                       follow_redirects=True)


def palette():
    r = client.get("/api/palette")
    return r, (r.get_json() or {})


# ---- 1. Logged out ------------------------------------------------------
client.get("/logout")
r, _ = palette()
check("1.1 Logged-out request is refused", r.status_code in (301, 302, 401, 403))

# ---- 2. Admin sees everything ------------------------------------------
login("pk_adm@x.com")
r, d = palette()
titles = [i["t"] for i in d.get("items", [])]
groups = {i["g"] for i in d.get("items", [])}
check("2.1 Admin request succeeds", r.status_code == 200 and d.get("ok") is True)
check("2.2 Admin gets admin destinations",
      "Admin Dashboard" in titles and "Daily Checklists" in titles)
check("2.3 Admin gets the staff directory", "Staff" in groups)
check("2.4 Every staff member is listed",
      all(n in titles for n in ("PK Picker", "PK Secret", "PK Driver", "PK Super")))
check("2.5 Staff entries link to their profile",
      any(i["u"] == "/staff/%d" % pick_id for i in d.get("items", [])))
check("2.6 Export actions offered", "Export CSV" in titles)
check("2.7 Every item has title, url and group",
      all(i.get("t") and i.get("u") and i.get("g") for i in d.get("items", [])))

# ---- 3. Ordinary staff see only their own things -----------------------
login("pk_p@x.com")
r, d = palette()
titles = [i["t"] for i in d.get("items", [])]
urls = [i["u"] for i in d.get("items", [])]
groups = {i["g"] for i in d.get("items", [])}
check("3.1 Staff request succeeds", r.status_code == 200 and d.get("ok") is True)
check("3.2 Staff get their own dashboard", "My Dashboard" in titles)
check("3.3 Staff do NOT get the admin dashboard", "Admin Dashboard" not in titles)
check("3.4 Staff do NOT get the checklist manager", "Daily Checklists" not in titles)
check("3.5 Staff do NOT get exports",
      "Export CSV" not in titles and "Export JSON" not in titles)
check("3.6 Staff do NOT get bulk import", "Bulk Import" not in titles)
check("3.7 No staff directory leaks to a colleague", "Staff" not in groups)
check("3.8 A colleague's name is not present", "PK Secret" not in titles)
check("3.9 A colleague's profile url is not present",
      not any(u.startswith("/staff/") and u != "/staff/%d" % pick_id for u in urls))
check("3.10 Own profile is offered", "/staff/%d" % pick_id in urls)
check("3.11 Picker is not offered the supervisor console",
      "Supervisor Console" not in titles)
check("3.12 Picker is not offered the delivery leaderboard",
      "Delivery Leaderboard" not in titles)

# ---- 4. Role-specific extras -------------------------------------------
login("pk_su@x.com")
_, d = palette()
check("4.1 Supervisor is offered their console",
      "Supervisor Console" in [i["t"] for i in d.get("items", [])])
check("4.2 Supervisor still gets no staff directory",
      "Staff" not in {i["g"] for i in d.get("items", [])})

login("pk_d@x.com")
_, d = palette()
check("4.3 Driver is offered the delivery leaderboard",
      "Delivery Leaderboard" in [i["t"] for i in d.get("items", [])])
check("4.4 Driver is not offered the admin dashboard",
      "Admin Dashboard" not in [i["t"] for i in d.get("items", [])])

# ---- 5. Always present --------------------------------------------------
check("5.1 Log out is always offered", "Log out" in [i["t"] for i in d.get("items", [])])

# ---- 6. The asset and markup are actually wired up ---------------------
login("pk_adm@x.com")
r = client.get("/admin/checklist")
check("6.1 Checklist page loads the palette script", b"js/palette.js" in r.data)
check("6.2 Checklist page has a touch trigger", b"ipk-trigger" in r.data)
r = client.get("/static/js/palette.js")
check("6.3 palette.js is served", r.status_code == 200 and b"__ipPalette" in r.data)
check("6.4 palette.js binds the Cmd+K chord", b"metaKey" in r.data and b"ctrlKey" in r.data)
check("6.5 palette.js escapes interpolated text", b"replace(/[&<>" in r.data)
r = client.get("/static/css/ip.css")
check("6.6 Palette styles are in the stylesheet",
      r.status_code == 200 and b".ipk__panel" in r.data)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
