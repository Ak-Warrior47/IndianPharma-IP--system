"""Team-wide pending bills, and urgent bills being worth more than normal.

Two rules are pinned down here.

1. PENDING IS A TEAM NUMBER.
   Every checker on duty types in the same figure for the day's total sales
   bills, then how many they personally cleared. What is left is the day's
   total minus what the whole team cleared:

       Lisa, Smriti and Deepti each enter "total 100".
       Lisa clears 30, Smriti 30, Deepti 30  ->  pending = 100 - 90 = 10.

   It used to be a number each checker typed in by hand and those were summed,
   so three checkers produced three guesses, pending came out at three times
   life size, and the clearance component of everyone's score sank with it.

2. URGENT BILLS PAY MORE THAN NORMAL ONES, AND NORMAL + URGENT <= TOTAL.
   Volume used to count an urgent bill exactly like a normal one, and the
   checker VWCR actually scored the NON-urgent share -- so taking the urgent
   work lowered your score. Nothing checked that urgent + normal stayed inside
   the day's total either, so a day could report 130 bills cleared out of 100
   and a negative pending pile.

Run:  python3.13 tests_checker_pending.py
"""
import os
import sys
from datetime import date, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import app, db, Employee, KPIEntry, MultitaskEntry  # noqa: E402
from tests_helper import csrf_client  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
DAY = date.today() - timedelta(days=40)   # well clear of any live data


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def mk(email, name, staff_type="checker", secondary=None):
    e = Employee.query.filter_by(email=email).first()
    if not e:
        e = Employee(name=name, email=email, staff_type=staff_type,
                     secondary_staff_type=secondary)
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    else:
        e.staff_type = staff_type
        e.secondary_staff_type = secondary
        db.session.commit()
    return e


def entry(emp, total, normal, urgent, items=0, urgent_items=0, on=DAY):
    KPIEntry.query.filter_by(emp_id=emp.id, entry_date=on).delete()
    db.session.commit()
    e = KPIEntry(emp_id=emp.id, entry_date=on,
                 bills_received=total,
                 sales_bills_open=normal, cs_sales_open=urgent,
                 checked=items, errors_found=urgent_items,
                 pending_bills_manual=0,
                 total_time=9.0, check_time=9.0)
    db.session.add(e)
    db.session.commit()
    return e


with app.app_context():
    db.create_all()

    lisa   = mk("lisa_t@x.com",   "Lisa")
    smriti = mk("smriti_t@x.com", "Smriti")
    deepti = mk("deepti_t@x.com", "Deepti")
    for e in (lisa, smriti, deepti):
        MultitaskEntry.query.filter_by(emp_id=e.id, entry_date=DAY).delete()
    db.session.commit()

    # ---- 1. The worked example from the shop floor ----------------------
    # Each enters total 100; each clears 30 (urgent + normal mixed).
    entry(lisa,   100, 20, 10, items=200, urgent_items=20)
    entry(smriti, 100, 25,  5, items=200, urgent_items=20)
    entry(deepti, 100, 30,  0, items=200, urgent_items=20)

    days = A.checker_team_day_totals([DAY])
    row = days[DAY]
    check("1.1 The day's total is 100, not 300 (three checkers, one figure)",
          row["total"] == 100, "got %s" % row["total"])
    check("1.2 The team cleared 90", row["checked"] == 90, "got %s" % row["checked"])
    check("1.3 Pending is 10", row["pending"] == 10, "got %s" % row["pending"])
    check("1.4 Each checker's own share is recorded",
          sorted(row["by_emp"].values()) == [30, 30, 30], "got %s" % row["by_emp"])

    # ---- 2. Every checker sees the same team pending --------------------
    stats = {}
    for e in (lisa, smriti, deepti):
        stats[e.name] = A.build_analytics(
            KPIEntry.query.filter_by(emp_id=e.id, entry_date=DAY).all(),
            "checker", emp_id=e.id)
    check("2.1 All three are told the same pending figure",
          {s["pending_bills"] for s in stats.values()} == {10},
          "got %s" % {k: v["pending_bills"] for k, v in stats.items()})
    check("2.2 Pending is no longer the hand-typed number",
          all(s["pending_manual"] == 0 for s in stats.values()))
    check("2.3 Clearance is the team's 90 of 100",
          {s["clearance_rate"] for s in stats.values()} == {90.0},
          "got %s" % {k: v["clearance_rate"] for k, v in stats.items()})
    check("2.4 Each is shown their own share of what the team cleared",
          all(abs(s["team_share_pct"] - 33.3) < 0.2 for s in stats.values()),
          "got %s" % {k: v["team_share_pct"] for k, v in stats.items()})
    check("2.5 The day's total is reported once, not summed per checker",
          {s["team_total_bills"] for s in stats.values()} == {100})

    # ---- 3. A second-role checker comes off the same pile ---------------
    # Someone checking as a SECOND role clears real bills, so pending must drop.
    rahul = mk("rahul_t@x.com", "Rahul", staff_type="picker", secondary="checker")
    MultitaskEntry.query.filter_by(emp_id=rahul.id, entry_date=DAY).delete()
    db.session.add(MultitaskEntry(emp_id=rahul.id, entry_date=DAY,
                                  secondary_type="checker",
                                  bills_received=100,
                                  sales_bills_open=6, cs_sales_open=4,
                                  checked=40, errors_found=5))
    db.session.commit()
    g_clear = A.checker_team_day_totals([DAY])
    check("3.1 A multitask checker's 10 bills come off the pending pile",
          g_clear[DAY]["pending"] == 0, "got %s" % g_clear[DAY]["pending"])
    check("3.2 The day's total is unchanged at 100",
          g_clear[DAY]["total"] == 100)

    # ---- 4. Urgent is worth more than normal ---------------------------
    # Two checkers, identical totals; one took the urgent work.
    MultitaskEntry.query.filter_by(emp_id=rahul.id, entry_date=DAY).delete()
    db.session.commit()
    OTHER = DAY - timedelta(days=1)
    only_normal = mk("normalonly_t@x.com", "Normal Only")
    took_urgent = mk("urgentone_t@x.com", "Urgent One")
    # Same 240 items and same 60 bills each -- only the urgent split differs.
    entry(only_normal, 200, 60, 0, items=240, urgent_items=0,  on=OTHER)
    entry(took_urgent, 200, 30, 30, items=120, urgent_items=120, on=OTHER)

    s_norm = A.build_analytics(
        KPIEntry.query.filter_by(emp_id=only_normal.id, entry_date=OTHER).all(),
        "checker", emp_id=only_normal.id)
    s_urg = A.build_analytics(
        KPIEntry.query.filter_by(emp_id=took_urgent.id, entry_date=OTHER).all(),
        "checker", emp_id=took_urgent.id)

    check("4.1 Urgent work is credited above its head-count",
          s_urg["weighted_items"] > s_urg["tck_total"],
          "%s vs %s" % (s_urg["weighted_items"], s_urg["tck_total"]))
    check("4.2 All-normal work is credited exactly at head-count",
          s_norm["weighted_items"] == s_norm["tck_total"],
          "%s vs %s" % (s_norm["weighted_items"], s_norm["tck_total"]))
    _b1 = A.build_analytics([KPIEntry(emp_id=None, entry_date=OTHER, bills_received=100,
        sales_bills_open=30, cs_sales_open=0, checked=200, total_time=9, check_time=9)], "checker")
    _b2 = A.build_analytics([KPIEntry(emp_id=None, entry_date=OTHER, bills_received=100,
        sales_bills_open=15, cs_sales_open=15, checked=200, total_time=9, check_time=9)], "checker")
    check("4.3b Urgent BILLS also earn more than normal bills (%s vs %s)"
          % (_b2["base_score"], _b1["base_score"]), _b2["base_score"] > _b1["base_score"])
    check("4.3 Same volume, but the one who took urgent scores higher",
          s_urg["eff_score"] > s_norm["eff_score"],
          "urgent %s vs normal %s" % (s_urg["eff_score"], s_norm["eff_score"]))
    check("4.4 Taking urgent work no longer drags VWCR down",
          s_urg["vwcr"] >= s_norm["vwcr"],
          "urgent %s vs normal %s" % (s_urg["vwcr"], s_norm["vwcr"]))
    check("4.5 The extra credit urgent earned is stated in plain items",
          s_urg["urgent_item_bonus"] > 0 and s_norm["urgent_item_bonus"] == 0)
    check("4.6 Weighted speed is at least the raw speed",
          s_urg["check_speed_weighted"] >= s_urg["check_speed"])

    # ---- 5. The point arithmetic is published --------------------------
    bd = s_urg["point_breakdown"]
    check("5.1 A point breakdown is returned", isinstance(bd, list) and len(bd) >= 8)
    check("5.2 Its maximums add to 100",
          sum(r["max"] for r in bd) == 100, "got %s" % sum(r["max"] for r in bd))
    check("5.3 Its awarded points add to the score",
          abs(sum(r["got"] for r in bd) - s_urg["base_score"]) < 0.3,
          "%s vs %s" % (sum(r["got"] for r in bd), s_urg["base_score"]))
    check("5.4 Every line says how it was worked out",
          all(r.get("how") for r in bd))
    check("5.5 The urgent weighting is named in the speed line",
          any("urgent" in r["how"].lower() for r in bd))
    check("5.6 The pending figure is named in the clearance line",
          any("pending" in r["how"].lower() for r in bd))
    _pk = A.build_analytics(
              [KPIEntry(emp_id=rahul.id, entry_date=OTHER, picked=100, missed=2,
                        total_bills_received=50, sales_bills_open=40,
                        total_time=9.0)],
              "picker", emp_id=rahul.id)
    check("5.7 Pickers get the same published breakdown, adding to their score",
          len(_pk["point_breakdown"]) == 4 and abs(sum(r["got"] for r in _pk["point_breakdown"]) - _pk["base_score"]) < 0.3)

    # ---- 6. Urgent + normal cannot exceed the day's total --------------
    CLEAN = DAY - timedelta(days=5)        # a day with nothing on it yet
    probs = A.checker_bill_problems(CLEAN, lisa.id, 100, 70, 40)
    check("6.1 110 checked out of 100 is rejected", bool(probs))
    check("6.2 The message explains urgent is inside the total",
          any("part of that total" in p for p in probs), "got %s" % probs)
    check("6.3 Exactly the total is allowed",
          A.checker_bill_problems(CLEAN, lisa.id, 100, 60, 40) == [],
          "got %s" % A.checker_bill_problems(CLEAN, lisa.id, 100, 60, 40))
    check("6.4 Under the total is allowed",
          A.checker_bill_problems(CLEAN, lisa.id, 100, 30, 10) == [])
    check("6.5 No total entered yet means nothing to contradict",
          A.checker_bill_problems(CLEAN, lisa.id, 0, 30, 10) == [])

    # The team ceiling is said out loud but does NOT refuse the entry: one
    # person mistyping the day's total must not lock everyone else out.
    # Lisa already has 60 of the day's 100.
    entry(lisa, 100, 50, 10, items=200, urgent_items=10, on=CLEAN)
    # The team as a whole cannot pass the day's total either: refused, and the
    # message says how many bills are actually left.
    check("6.6 Pushing the team past the day's total is refused",
          bool(A.checker_bill_problems(CLEAN, smriti.id, 100, 30, 20)))
    check("6.7 The refusal says what the others entered and what is left",
          any("other checkers" in w and "Only 40" in w
              for w in A.checker_bill_problems(CLEAN, smriti.id, 100, 30, 20)))
    check("6.8 What still fits is accepted",
          A.checker_bill_problems(CLEAN, smriti.id, 100, 30, 10) == [],
          "got %s" % A.checker_bill_problems(CLEAN, smriti.id, 100, 30, 10))
    check("6.9 Re-saving your own entry does not count you twice",
          A.checker_bill_problems(CLEAN, lisa.id, 100, 50, 10) == [])
    check("6.10 A checker typing the correct HIGHER total is never blocked by a low one",
          A.checker_bill_problems(CLEAN, smriti.id, 150, 30, 20) == [])

# ---- 6b. Old days, where each checker typed their OWN bills ----------
# The form used to say "All bills given to you today", so older days hold
# each checker's own figure. Taking the biggest as the day total then shows
# more bills checked than existed; the only reading that adds up is the sum.
with app.app_context():
    OLD = DAY - timedelta(days=12)
    for em, rec, n, u in (("lisa_t@x.com", 172, 75, 42), ("smriti_t@x.com", 90, 60, 20),
                          ("deepti_t@x.com", 70, 50, 10)):
        e = Employee.query.filter_by(email=em).first()
        entry(e, rec, n, u, items=300, urgent_items=40, on=OLD)
    od = A.checker_team_day_totals([OLD])[OLD]
    check("6b.1 An own-bills day is recognised", od["mode"] == "per_person", od["mode"])
    check("6b.2 Its total is the checkers' figures added: 332", od["total"] == 332)
    check("6b.3 Pending = 332 - 257 checked = 75", od["pending"] == 75, od["pending"])
    nd = A.checker_team_day_totals([DAY])[DAY]
    check("6b.4 A proper day-total day is untouched (pending 10)", nd["mode"] == "team" and nd["pending"] == 10)
src = open(os.path.join(REPO, "templates", "staff_detail.html")).read()
check("6b.5 Period buttons can hide the other periods", ".period-panel { display: none; }" in src)
check("6b.6 The daily log uses the team pending, not the typed one",
      "team_days.get(e.entry_date)" in src and "e.bills_received or 0) - e_sb" not in src)
for t in ("dashboard.html", "past_entry.html"):
    f = open(os.path.join(REPO, "templates", t)).read()
    check("6b.7 %s asks for the whole day's total, not 'your' bills" % t,
          "Day's Total Sales Bills (whole team)" in f and "All bills given to you" not in f)

# ---- 7. The form refuses it too ----------------------------------------
# Via the real past-entry window, as Lisa herself, so the whole route runs.
from app import PastEntryWindow  # noqa: E402

PAST = DAY - timedelta(days=7)
while PAST.weekday() == 6:          # the app refuses Sunday entries
    PAST -= timedelta(days=1)
with app.app_context():
    lisa_id = Employee.query.filter_by(email="lisa_t@x.com").first().id
    KPIEntry.query.filter_by(entry_date=PAST).delete()
    _w = PastEntryWindow.query.filter_by(past_date=PAST).first()
    if _w:
        _w.is_active = True      # another suite's "Close All Days" may have closed it
    else:
        db.session.add(PastEntryWindow(past_date=PAST, is_active=True))
    db.session.commit()

client = csrf_client(app.test_client())
client.post("/login", data={"email": "lisa_t@x.com", "password": PW},
            follow_redirects=True)

FORM = {"bills_received": "100", "checked": "150", "errors_found": "20",
        "picked": "0", "packing_done": "0", "sweep_done": "0"}

r = client.post("/past_entry/%s" % PAST.isoformat(),
                data=dict(FORM, sales_bills_open="70", cs_sales_open="40"),
                follow_redirects=True)
check("7.1 The past-entry form refuses bills over the day's total",
      b"more than the day" in r.data, "got %r" % r.data[-400:])
with app.app_context():
    saved = KPIEntry.query.filter_by(emp_id=lisa_id, entry_date=PAST).first()
check("7.2 Nothing was written", saved is None)

r = client.post("/past_entry/%s" % PAST.isoformat(),
                data=dict(FORM, sales_bills_open="60", cs_sales_open="40"),
                follow_redirects=True)
with app.app_context():
    saved = KPIEntry.query.filter_by(emp_id=lisa_id, entry_date=PAST).first()
check("7.3 Figures that add up are saved", saved is not None)

with app.app_context():
    KPIEntry.query.filter_by(entry_date=PAST).delete()
    PastEntryWindow.query.filter_by(past_date=PAST).delete()
    db.session.commit()

# ---- cleanup -----------------------------------------------------------
with app.app_context():
    for em in ("lisa_t@x.com", "smriti_t@x.com", "deepti_t@x.com", "rahul_t@x.com",
               "normalonly_t@x.com", "urgentone_t@x.com"):
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
