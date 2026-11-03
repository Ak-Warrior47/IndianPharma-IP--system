"""CSRF protection: every state-changing request needs the session's token.

Without this, any page on the internet can make a logged-in admin's browser
submit a form here -- deleting an employee, awarding points, opening a past
entry window -- because the session cookie rides along automatically.

What matters is not that a token exists but that a request WITHOUT one is
refused, so most of this file submits forms the way an attacker's page would
and asserts nothing happened.

Run:  python3.13 tests_csrf.py
"""
import os
import re
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee, ChecklistTemplate  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + detail) if detail and not cond else ""))


with app.app_context():
    db.create_all()

    def mk(name, email, staff_type, admin=False):
        e = Employee.query.filter_by(email=email).first()
        if e:
            return e
        e = Employee(name=name, email=email, staff_type=staff_type,
                     is_admin=admin, sunday_override=True)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
        return e

    mk("CS Admin", "cs_adm@x.com", "picker", admin=True)
    victim = mk("CS Victim", "cs_victim@x.com", "picker")
    victim_id = victim.id

client = app.test_client()


def token(path="/login"):
    """The token a real page would render.

    Read from the meta tag, which every page carries, because /login redirects
    once a session exists and would hand back nothing.
    """
    html = client.get(path, follow_redirects=True).data.decode()
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not m:
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def login(email):
    client.get("/logout")
    t = token()
    return client.post("/login", data={"email": email, "password": PW,
                                       "_csrf_token": t}, follow_redirects=True)


# ---- 1. The token is actually rendered ---------------------------------
t = token()
check("1.1 Login form carries a CSRF token", bool(t) and len(t) > 20)
html = client.get("/login").data.decode()
check("1.2 Pages expose the token for fetch() via a meta tag",
      'name="csrf-token"' in html)
t2 = token()
check("1.3 The token is stable within a session", t == t2)

# ---- 2. Login itself is protected --------------------------------------
client.get("/logout")
r = client.post("/login", data={"email": "cs_adm@x.com", "password": PW},
                follow_redirects=True)
check("2.1 Login without a token is refused", b"Add New Employee" not in r.data)
check("2.2 Login with a token works",
      b"Add New Employee" in login("cs_adm@x.com").data)

# ---- 3. A forged request changes nothing -------------------------------
# The admin is logged in; this is what a malicious page would be able to make
# their browser send -- everything except the token it cannot read.
r = client.post("/admin/delete_user/%d" % victim_id, follow_redirects=True)
with app.app_context():
    still_there = db.session.get(Employee, victim_id) is not None
check("3.1 Employee delete without a token does nothing", still_there)

r = client.post("/admin/add_user", data={"name": "Injected", "email": "inj@x.com",
                                         "password": PW, "staff_type": "picker"},
                follow_redirects=True)
with app.app_context():
    check("3.2 Adding a user without a token does nothing",
          Employee.query.filter_by(email="inj@x.com").first() is None)

r = client.post("/admin/checklist", data={"action": "add", "staff_type": "picker",
                                          "text": "Injected task"},
                follow_redirects=True)
with app.app_context():
    check("3.3 Adding a checklist task without a token does nothing",
          ChecklistTemplate.query.filter_by(text="Injected task").first() is None)

r = client.post("/admin/edit_user/%d" % victim_id,
                data={"name": "Renamed By Attacker", "email": "cs_victim@x.com",
                      "staff_type": "picker"}, follow_redirects=True)
with app.app_context():
    v = db.session.get(Employee, victim_id)
check("3.4 Editing a user without a token does nothing",
      v is not None and v.name == "CS Victim")

check("3.5 The user is told why, not left guessing",
      b"session expired" in r.data.lower() or b"try that again" in r.data.lower())

# ---- 4. A wrong or stale token is refused ------------------------------
r = client.post("/admin/add_user",
                data={"name": "Wrong Token", "email": "wrong@x.com",
                      "password": PW, "staff_type": "picker",
                      "_csrf_token": "not-the-right-token"}, follow_redirects=True)
with app.app_context():
    check("4.1 A wrong token is refused",
          Employee.query.filter_by(email="wrong@x.com").first() is None)

# ---- 5. With the right token, everything still works -------------------
good = token("/admin_dashboard")
r = client.post("/admin/add_user",
                data={"name": "Legit User", "email": "legit@x.com",
                      "password": PW, "staff_type": "picker",
                      "_csrf_token": good}, follow_redirects=True)
with app.app_context():
    check("5.1 A correct token lets the request through",
          Employee.query.filter_by(email="legit@x.com").first() is not None)

r = client.post("/admin/checklist", data={"action": "add", "staff_type": "picker",
                                          "text": "Legit task",
                                          "_csrf_token": good},
                follow_redirects=True)
with app.app_context():
    check("5.2 Checklist add works with a token",
          ChecklistTemplate.query.filter_by(text="Legit task").first() is not None)

# ---- 6. JSON endpoints: header and body both accepted ------------------
r = client.post("/checklist/toggle", json={"template_id": 1, "done": True})
check("6.1 A JSON POST without a token is refused", r.status_code == 400)
body = r.get_json() or {}
check("6.2 The JSON refusal is machine-readable", body.get("ok") is False)

with app.app_context():
    tid = ChecklistTemplate.query.filter_by(text="Legit task").first().id
login("cs_adm@x.com")
good = token("/admin_dashboard")
r = client.post("/checklist/toggle", json={"template_id": tid, "done": True},
                headers={"X-CSRFToken": good})
check("6.3 A JSON POST with the header is accepted", r.status_code != 400)

# ---- 7. Every POST form in every template carries the field ------------
import glob  # noqa: E402
forms = fields = 0
missing = []
for p in sorted(glob.glob(os.path.join(REPO, "templates", "*.html"))):
    s = open(p).read()
    f = len(re.findall(r'<form\b[^>]*method\s*=\s*["\']?post["\']?[^>]*>', s, re.I))
    c = s.count("_csrf_token")
    forms += f
    fields += c
    if f > c:
        missing.append("%s (%d forms, %d fields)" % (os.path.basename(p), f, c))
check("7.1 Every POST form carries the token field (%d forms)" % forms,
      not missing, "; ".join(missing))
check("7.2 fetch() is patched to send the header",
      "X-CSRFToken" in open(os.path.join(REPO, "static/js/ux.js")).read())

# ---- 8. GET is never blocked -------------------------------------------
login("cs_adm@x.com")
for path in ("/admin_dashboard", "/admin/checklist", "/admin/diagnostics", "/dashboard"):
    if client.get(path).status_code >= 400:
        check("8.1 GET requests are not blocked by CSRF", False, path)
        break
else:
    check("8.1 GET requests are not blocked by CSRF", True)

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
