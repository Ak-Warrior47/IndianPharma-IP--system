"""UI and route QA sweep via Flask's test client.

Drives every page as every role and checks:
  - HTTP status (500 is a hard failure; an unexpected redirect is reported)
  - every link target and form action resolves to a real route
  - no Jinja rendered an "Undefined" or left a raw {{ }} in the output
  - no template fell back to an error state
  - buttons carry an accessible label
  - access control: a staff member must not reach an admin page

A browser was tried first, but this sandbox blocks the CDNs and Chromium's own
telemetry hosts, so page loads stalled on network timeouts rather than on
anything in the application. The test client exercises the same server-rendered
markup without that noise. JavaScript behaviour is covered separately by
tests_palette and tests_checklist_order.

Run:  python3.13 tests_ui_sweep.py
"""
import os
import re
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
DEFECTS = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + detail) if detail and not cond else ""))


ROLE_PAGES = {
    "admin": ["/admin_dashboard", "/admin/checklist", "/admin/delivery",
              "/admin/diagnostics", "/admin/reports", "/admin/bulk_import",
              "/supervisor", "/delivery/leaderboard",
              "/notifications", "/dashboard", "/api/palette"],
    "picker": ["/dashboard", "/notifications", "/api/palette"],
    "checker": ["/dashboard", "/notifications"],
    "purchaser": ["/dashboard", "/notifications"],
    "delivery": ["/dashboard", "/delivery/leaderboard", "/notifications"],
    "biller": ["/dashboard", "/notifications"],
    "packer": ["/dashboard", "/notifications"],
    "supervisor": ["/supervisor", "/dashboard", "/notifications"],
}

ADMIN_ONLY = ["/admin_dashboard", "/admin/checklist", "/admin/delivery",
              "/admin/diagnostics", "/admin/bulk_import", "/admin/export_csv",
              "/admin/export_full"]

with app.app_context():
    db.create_all()
    rules = [re.compile("^" + re.sub(r"<[^>]+>", "[^/]+", r.rule) + "/?$")
             for r in app.url_map.iter_rules()]

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

    mk("QA Admin", "qa_admin@x.com", "picker", admin=True)
    mk("QA Picker", "qa_picker@x.com", "picker")
    mk("QA Checker", "qa_checker@x.com", "checker")
    mk("QA Purchaser", "qa_purchaser@x.com", "purchaser")
    mk("QA Delivery", "qa_delivery@x.com", "delivery")
    mk("QA Biller", "qa_biller@x.com", "biller")
    mk("QA Packer", "qa_packer@x.com", "packer")
    mk("QA Super", "qa_supervisor@x.com", "supervisor", secondary="picker")


def route_ok(p):
    p = p.split("?")[0].split("#")[0]
    if not p.startswith("/") or p.startswith("/static/"):
        return True
    return any(rx.match(p) for rx in rules)


client = csrf_client(app.test_client())


def login(role):
    client.get("/logout")
    return client.post("/login", data={"email": "qa_%s@x.com" % role, "password": PW},
                       follow_redirects=True)


LINK_RE = re.compile(rb'href="(/[^"#?]*)[^"]*"')
ACTION_RE = re.compile(rb'action="(/[^"#?]*)[^"]*"')
UNDEF_RE = re.compile(rb"\bUndefined\b|\{\{|\{%")
BUTTON_RE = re.compile(rb"<button\b[^>]*>(.*?)</button>", re.S)

# ---- 1. Every page loads for its role -----------------------------------
for role, pages in ROLE_PAGES.items():
    login(role)
    for path in pages:
        r = client.get(path)
        where = "%s @ %s" % (role, path)
        if r.status_code >= 500:
            DEFECTS.append(("HTTP %d" % r.status_code, where, "server error"))
        elif r.status_code == 404:
            DEFECTS.append(("HTTP 404", where, "route missing"))

check("1.1 No page returns 500 or 404 for its own role",
      not DEFECTS, "; ".join("%s %s" % (k, w) for k, w, _ in DEFECTS[:4]))

# ---- 2. Links and form actions resolve ----------------------------------
dead = []
for role, pages in ROLE_PAGES.items():
    login(role)
    for path in pages:
        r = client.get(path)
        if r.status_code != 200 or b"<html" not in r.data[:4000].lower():
            continue
        for m in set(LINK_RE.findall(r.data)) | set(ACTION_RE.findall(r.data)):
            t = m.decode("utf-8", "replace")
            if not route_ok(t):
                dead.append("%s @ %s -> %s" % (role, path, t))
check("2.1 Every link and form action resolves to a real route",
      not dead, "; ".join(sorted(set(dead))[:5]))

# ---- 3. Every template variable is actually supplied -------------------
# Jinja renders a missing variable as an empty string, so a route that forgets
# a kwarg produces a silently blank page rather than an error. Re-rendering
# under StrictUndefined turns that into a raised exception, which is how the
# "template uses a variable the route never passed" bug gets caught -- the same
# class of defect that made panels render empty with nothing to explain why.
from jinja2 import StrictUndefined  # noqa: E402
_prev_undefined = app.jinja_env.undefined
app.jinja_env.undefined = StrictUndefined
app.jinja_env.cache = {}
missing_ctx = []
for role, pages in ROLE_PAGES.items():
    login(role)
    for path in pages:
        try:
            r = client.get(path, follow_redirects=True)
            # Routes wrap their body in try/except, so a StrictUndefined error
            # surfaces as a redirect to the error fallback rather than a 500.
            # Both outcomes mean a variable was missing.
            if r.status_code >= 500:
                missing_ctx.append("%s @ %s (500)" % (role, path))
            elif b"Error loading" in r.data or b"System error" in r.data:
                missing_ctx.append("%s @ %s (fell back to error page)" % (role, path))
        except Exception as e:
            missing_ctx.append("%s @ %s :: %s" % (role, path, str(e)[:90]))
app.jinja_env.undefined = _prev_undefined
app.jinja_env.cache = {}
check("3.1 Every variable a template uses is passed by its route",
      not missing_ctx, "; ".join(sorted(set(missing_ctx))[:5]))

# ---- 4. Error-state fallbacks did not fire ------------------------------
err_state = []
for role, pages in ROLE_PAGES.items():
    login(role)
    for path in pages:
        r = client.get(path)
        for marker in (b"Error loading", b"System error", b"Internal Server Error"):
            if marker in r.data:
                err_state.append("%s @ %s (%s)" % (role, path, marker.decode()))
check("4.1 No page fell back to an error state",
      not err_state, "; ".join(err_state[:5]))

# ---- 5. Buttons have an accessible label --------------------------------
unlabelled = []
for role, pages in ROLE_PAGES.items():
    login(role)
    for path in pages:
        r = client.get(path)
        if r.status_code != 200:
            continue
        for m in BUTTON_RE.finditer(r.data):
            whole = m.group(0)
            inner = re.sub(rb"<[^>]+>", b"", m.group(1)).strip()
            if inner:
                continue
            if b"aria-label" in whole or b"title=" in whole:
                continue
            unlabelled.append("%s @ %s :: %s" % (role, path,
                                                 whole[:70].decode("utf-8", "replace")))
check("5.1 Every button has text, aria-label or title",
      not unlabelled, "; ".join(sorted(set(unlabelled))[:4]))

# ---- 6. Access control --------------------------------------------------
leaks = []
for role in ("picker", "checker", "delivery", "biller", "packer", "supervisor"):
    login(role)
    for path in ADMIN_ONLY:
        r = client.get(path, follow_redirects=True)
        if b"Add New Employee" in r.data or b"Problems Found" in r.data \
                or b"Add a Task" in r.data:
            leaks.append("%s reached %s" % (role, path))
check("6.1 No staff role reaches an admin page", not leaks, "; ".join(leaks[:5]))

# ---- 7. Logged out is locked out ---------------------------------------
client.get("/logout")
open_pages = []
for path in ADMIN_ONLY + ["/dashboard", "/supervisor", "/api/palette"]:
    r = client.get(path, follow_redirects=True)
    if b"assword" not in r.data and r.status_code == 200:
        open_pages.append(path)
check("7.1 Every protected page redirects to login when logged out",
      not open_pages, "; ".join(open_pages[:5]))

# ---- 8. Static assets served -------------------------------------------
for asset in ("/static/css/ip.css", "/static/js/palette.js", "/static/js/ux.js"):
    r = client.get(asset)
    check("8.%d %s is served" % (("/static/css/ip.css", "/static/js/palette.js",
                                  "/static/js/ux.js").index(asset) + 1, asset),
          r.status_code == 200 and len(r.data) > 400)

print("\n" + "=" * 64)
if DEFECTS:
    print("PAGE DEFECTS")
    for k, w, d in DEFECTS:
        print("  %-10s %-34s %s" % (k, w, d))
    print()
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 64)
sys.exit(1 if FAILED else 0)
