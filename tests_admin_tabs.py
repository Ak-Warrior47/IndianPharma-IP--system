"""The admin dashboard, cut into tabs.

The page was seven screens of scroll with everything at the same weight: the
announcement form, used once a month, sat above the staff table, opened every
day, and the "Add Employee" and "Open Past Date" forms were parked in the
middle of the data. Same panels, same features, four groups.

What has to stay true:
  * every panel belongs to exactly one tab -- a panel with no tab is a feature
    that silently disappears from the admin panel
  * Staff opens first, with the staff table above the Add Employee form
  * nothing was deleted on the way

Run:  python3.13 tests_admin_tabs.py
"""
import os
import re
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from app import app, db, Employee, MultitaskEntry  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
TABS = ("staff", "today", "scores", "admin")


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


with app.app_context():
    db.create_all()
    a = Employee.query.filter_by(email="tab_adm@x.com").first()
    if not a:
        a = Employee(name="Tab Admin", email="tab_adm@x.com",
                     staff_type="picker", is_admin=True)
        a.set_password(PW)
        db.session.add(a)
        db.session.commit()

client = csrf_client(app.test_client())
client.post("/login", data={"email": "tab_adm@x.com", "password": PW},
            follow_redirects=True)
r = client.get("/admin_dashboard")
html = r.data.decode("utf-8", "replace")
# The stylesheet names the same attributes in its selectors, so counting
# attributes over the whole page counts the CSS rules too. Markup only.
body = re.sub(r"<style[\s\S]*?</style>", "", html)

# ---- 1. The tab bar -----------------------------------------------------
check("1.1 The admin dashboard loads", r.status_code == 200)
check("1.2 There is a tab for each of the four groups",
      all(('data-tab="%s"' % t) in html for t in TABS))
check("1.3 The tab bar is announced to screen readers",
      'role="tablist"' in html and html.count('role="tab"') >= 4)
check("1.4 Staff is the tab that opens first",
      'class="main" data-tab="staff"' in html)
check("1.5 The open tab is marked selected, the rest are not",
      body.count('aria-selected="true"') == 1 and
      body.count('aria-selected="false"') == 3)

# ---- 2. Every panel is in exactly one tab -------------------------------
# Assigned in the template: a panel that renders only when it has content
# (Pending Past Entry Requests) is still a panel that must belong to a tab.
SRC = open(os.path.join(REPO, "templates", "admin.html")).read()
SRC_BODY = re.sub(r"<style[\s\S]*?</style>", "", SRC)
src_panes = re.findall(r'data-pane="([a-z]+)"', SRC_BODY)
panes = re.findall(r'data-pane="([a-z]+)"', body)
check("2.1 All fourteen panels are assigned to a tab", len(src_panes) == 14,
      "found %d" % len(src_panes))
check("2.2 No panel names a tab that does not exist",
      set(src_panes) <= set(TABS), "stray: %s" % (set(src_panes) - set(TABS)))
check("2.3 Every tab has something in it",
      set(src_panes) == set(TABS), "empty: %s" % (set(TABS) - set(src_panes)))
check("2.4 Each panel also declares its order within its tab",
      len(re.findall(r'data-ord="\d+"', SRC_BODY)) == 14)
check("2.5 Nothing rendered on the page sits outside a tab",
      set(panes) <= set(TABS) and len(panes) >= 10,
      "rendered %d: %s" % (len(panes), set(panes)))

# ---- 3. Nothing was lost in the move ------------------------------------
PANELS = [
    b"Staff Management", b"Add New Employee", b"Open Past Date Entry",
    b"Announcements / Notice Board", b"Multitask (Role 2) Reports",
    b"Delivery &amp; Biller Daily Notes", b"Badge Management",
    b"Post-Delivery Complaint", b"Scorecard &amp; Leaderboard",
    b"Performance Summary", b"Team Health",
]
missing = [p for p in PANELS if p not in r.data]
check("3.1 Every panel is still on the page (%s)"
      % (b", ".join(missing).decode() if missing else "none missing"),
      not missing)
check("3.2 The staff table is still there with its rows",
      b'id="staffTable"' in r.data)
check("3.3 The search box still filters it", b'id="searchBox"' in r.data)

# ---- 4. Order inside the Staff tab --------------------------------------
# The table is what this page is opened for; the form comes after it.
m_table = re.search(r'data-pane="staff" data-ord="(\d+)"[\s\S]*?Staff Management', html)
staff_blocks = re.findall(r'data-pane="staff" data-ord="(\d+)"', html)
check("4.1 Both Staff panels carry an order", len(staff_blocks) == 2)
ord_of = {}
for mm in re.finditer(r'data-pane="staff" data-ord="(\d+)"([\s\S]{0,900})', html):
    body = mm.group(2)
    if "Staff Management" in body:
        ord_of["table"] = int(mm.group(1))
    elif "Add New Employee" in body:
        ord_of["form"] = int(mm.group(1))
check("4.2 The staff table is ordered above the Add Employee form",
      ord_of.get("table", 9) < ord_of.get("form", 0), "got %s" % ord_of)

# ---- 5. Panels outside the open tab are not laid out --------------------
check("5.1 Closed tabs are hidden by CSS, not left stacked down the page",
      ".main[data-tab] > [data-pane] { display: none; }" in html)
check("5.2 Each tab's rule reveals only its own panels",
      all(('.main[data-tab="%s"]' % t) in html for t in TABS))
check("5.3 The default tab is set in the markup, so nothing flashes on load",
      'data-tab="staff"' in html.split("<style")[0] or
      'class="main" data-tab="staff"' in html)

# ---- 6. Alerts and the headline figures stay above the tabs -------------
check("6.1 Flash messages and the overview figures sit outside the tabs",
      ".alert, .overview-grid { order: -2; }" in html)
check("6.2 The overview figures are still rendered",
      b"Total Staff" in r.data and b"Active Today" in r.data)

# ---- 7. The tables use the design system's table ------------------------
# Left bare, they fell back to browser defaults, which is why the header read
# as one run-together phrase: "Entries Monthly Score November 2026 Week Score".
check("7.1 The staff table uses the styled table class",
      'id="staffTable" class="ptable"' in html)
check("7.2 No table on the page is left unstyled",
      not re.search(r"<table(?![^>]*ptable)[^>]*>", html),
      re.findall(r"<table[^>]*>", html).__str__())
check("7.3 The month is a sub-line under the column name, not trailing it",
      'class="th-sub"' in html)

# ---- 8. A multitask row of zeros says nothing ---------------------------
with app.app_context():
    e = MultitaskEntry(secondary_type="purchaser")
    check("8.1 An entry with no figures says so, instead of printing zeros",
          e.summary == "No figures entered", "got %r" % e.summary)
    e2 = MultitaskEntry(secondary_type="purchaser", checked=7, picked=0,
                        sales_bills_open=0, errors_found=3)
    check("8.2 Only the figures that carry work are printed",
          e2.summary == "PO Checked: 7 · Items: 3", "got %r" % e2.summary)
    e3 = MultitaskEntry(secondary_type="delivery", quantity=12)
    check("8.3 Delivery still reports its quantity",
          e3.summary == "Qty Done: 12", "got %r" % e3.summary)
    e4 = MultitaskEntry(secondary_type="checker", bills_received=100,
                        sales_bills_open=60, cs_sales_open=20, checked=0)
    check("8.4 A checker's zero item count drops out, the rest stay",
          e4.summary == "Bills Rcvd: 100 · SB Checked: 60 · CS Checked: 20",
          "got %r" % e4.summary)

# ---- 9. Staff still cannot reach the admin panel ------------------------
client.get("/logout")
with app.app_context():
    s = Employee.query.filter_by(email="tab_staff@x.com").first()
    if not s:
        s = Employee(name="Tab Staff", email="tab_staff@x.com", staff_type="picker")
        s.set_password(PW)
        db.session.add(s)
        db.session.commit()
client.post("/login", data={"email": "tab_staff@x.com", "password": PW},
            follow_redirects=True)
rs = client.get("/admin_dashboard", follow_redirects=True)
check("9.1 A staff member cannot open the admin dashboard",
      b"Staff Management" not in rs.data)

with app.app_context():
    for em in ("tab_adm@x.com", "tab_staff@x.com"):
        e = Employee.query.filter_by(email=em).first()
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
