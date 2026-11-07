"""The "@pharmaip.com" email pop-up, on every page that asks for an email.

Typing "lisa" offers "lisa@pharmaip.com"; Enter, Tab or a tap takes it. An
address on another domain is left alone. The pop-up lives in one shared file,
static/js/email-suggest.js, so the rule tested here is simple: any template
with an email box must load that file.

Run:  python3.13 tests_email_suggest.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
JS = os.path.join(REPO, "static", "js", "email-suggest.js")
EMAIL_BOX = re.compile(r"<input[^>]*(type=\"email\"|name=\"email\")", re.I)


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


# ---- 1. Every page with an email box loads the pop-up -------------------
tdir = os.path.join(REPO, "templates")
with_box = []
for fn in sorted(os.listdir(tdir)):
    if not fn.endswith(".html"):
        continue
    src = open(os.path.join(tdir, fn), encoding="utf-8").read()
    if EMAIL_BOX.search(src):
        with_box.append(fn)
        check("1. %s has an email box and loads email-suggest.js" % fn,
              "js/email-suggest.js" in src)
check("1.x The login and admin pages are among them",
      {"login.html", "admin.html"} <= set(with_box), str(with_box))
check("1.y The script file exists", os.path.isfile(JS))

# ---- 2. It is served, and the pages really include it --------------------
with app.app_context():
    db.create_all()
    a = Employee.query.filter_by(email="es_t_adm@x.com").first()
    if not a:
        a = Employee(name="ES Admin", email="es_t_adm@x.com", staff_type="picker", is_admin=True)
        a.set_password(PW)
        db.session.add(a)
        db.session.commit()

c = csrf_client(app.test_client())
r = c.get("/static/js/email-suggest.js")
check("2.1 The script is served", r.status_code == 200 and b"pharmaip.com" in r.data)
r.close()
check("2.2 The login page includes it", b"js/email-suggest.js" in c.get("/login").data)
c.post("/login", data={"email": "es_t_adm@x.com", "password": PW}, follow_redirects=True)
adm = c.get("/admin_dashboard").data
check("2.3 The admin page includes it", b"js/email-suggest.js" in adm)
check("2.4 The Add Employee email box is a real email field",
      re.search(rb'<input type="email" name="email" id="addEmail"', adm) is not None)
check("2.5 The old broken add-form helper is gone", b"handleAddEmail" not in adm)

# ---- 3. The suggestion rule itself (run in node when it is installed) ----
CASES = [
    ("lisa", "lisa@pharmaip.com"),
    ("  lisa  ", "lisa@pharmaip.com"),
    ("lisa@", "lisa@pharmaip.com"),
    ("lisa@ph", "lisa@pharmaip.com"),
    ("lisa@PHARM", "lisa@pharmaip.com"),
    ("lisa@pharmaip.com", None),       # already complete
    ("raj@gmail.com", None),           # another domain: leave it
    ("raj@gm", None),
    ("", None),
    ("two words", None),
    ("@pharmaip.com", None),
    ("a@b@c", None),
]
node = shutil.which("node")
if node:
    prog = (
        "global.window={};global.document={readyState:'complete',querySelectorAll:()=>[],"
        "getElementById:()=>null,documentElement:{}};"
        "require(%s);const f=window.EmailSuggest.suggestionFor;"
        "console.log(JSON.stringify(%s.map(c=>f(c,'pharmaip.com'))));"
    ) % (json.dumps(JS), json.dumps([x for x, _ in CASES]))
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    got = json.loads(out.stdout.strip() or "[]") if out.returncode == 0 else []
    check("3.0 The script runs", out.returncode == 0, out.stderr[-300:])
    for (inp, want), g in zip(CASES, got or [None] * len(CASES)):
        check("3. %r -> %r" % (inp, want), g == want, "got %r" % g)
else:
    print("SKIP  3. node is not installed, rule checked in the browser test only")

with app.app_context():
    e = Employee.query.filter_by(email="es_t_adm@x.com").first()
    if e:
        db.session.delete(e)
        db.session.commit()

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
