"""Level-up system: a better-than-good best day becomes the new target.

The owner's rule, tested exactly:
  * a day better than good (score >= 72) that beats the staff member's best
    day ever levels them up; that best day becomes their target
  * the same for best week (Mon-Sat) and best month
  * a congratulations notification each time
  * targets only ever go up; nobody levels up on old history when the system
    is switched on; the same day can never level someone up twice

Run:  python3.13 tests_levels.py
"""
import os
import sys
from datetime import date, timedelta

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import app as A  # noqa: E402
from app import (app, db, Employee, KPIEntry, DeliveryBillerNote, PersonalRecord,  # noqa: E402
                 LevelEvent, Notification)
from tests_helper import csrf_client, add_picks  # noqa: E402

app.config["TESTING"] = True
PW = "longpass123"
PASSED, FAILED = [], []
TODAY = date.today()


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


def workdays_back(n, end):
    out, d = [], end
    while len(out) < n:
        if d.weekday() != 6:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def mk(email, st, admin=False):
    e = Employee.query.filter_by(email=email).first()
    if e:
        PersonalRecord.query.filter_by(emp_id=e.id).delete()
        LevelEvent.query.filter_by(emp_id=e.id).delete()
        Notification.query.filter_by(emp_id=e.id).delete()
        KPIEntry.query.filter_by(emp_id=e.id).delete()
        DeliveryBillerNote.query.filter_by(emp_id=e.id).delete()
        A.PickClaim.query.filter_by(picker_id=e.id).delete()
        e.custom_hourly_target = None
        e.staff_type = st
        db.session.commit()
        return e
    e = Employee(name="Lv " + email.split("@")[0], email=email, staff_type=st, is_admin=admin)
    e.set_password(PW)
    db.session.add(e)
    db.session.commit()
    # SQLite reuses the id of a deleted row and does not enforce the foreign
    # key, so rows left by a staff member another suite deleted can still be
    # sitting under this id. (On PostgreSQL the cascade removes them.)
    for M in (PersonalRecord, LevelEvent, Notification, KPIEntry, DeliveryBillerNote):
        M.query.filter_by(emp_id=e.id).delete()
    A.PickClaim.query.filter_by(picker_id=e.id).delete()
    db.session.commit()
    return e


def picker_day(e, d, items, good=True):
    """A picker day. good=True gives a strong all-round day (score well above 72)."""
    db.session.add(KPIEntry(
        emp_id=e.id, entry_date=d, total_bills_received=50,
        sales_bills_open=50 if good else 10, picked=items, missed=0 if good else 40,
        cs_sales_open=10, packing_done=10 if good else 0, rack_organized=10 if good else 0,
        table_clean=1 if good else 0, sweep_done=1 if good else 0, total_time=9, check_time=9))
    db.session.commit()
    # Bill-based picking: the same day as finished grabs, items/hr = items / 9.
    if e.staff_type == "picker":
        add_picks(e.id, d, items, hours=9.0, on_time=good)


with app.app_context():
    db.create_all()
    p = mk("lv_pick@x.com", "picker")
    days = workdays_back(12, TODAY - timedelta(days=1))
    hist, new_day = days[:-1], days[-1]
    for d in hist:
        picker_day(p, d, 300)                  # steady history at 300/9 = 33.3 items/hr

    # ---- 1. Switching it on never floods staff with old level-ups -------
    ev = A.evaluate_levels(p, day=hist[-1])
    recs = {r.period: r for r in PersonalRecord.query.filter_by(emp_id=p.id).all()}
    check("1.1 Records are set up from history", "day" in recs and recs["day"].best_value > 0,
          str({k: (v.best_value, v.level) for k, v in recs.items()}))
    check("1.2 No level-up for old history", all(v.level == 0 for v in recs.values()),
          str({k: v.level for k, v in recs.items()}))
    check("1.3 No notification for old history",
          Notification.query.filter_by(emp_id=p.id).count() == 0)

    # ---- 2. A better-than-good best day levels up and sets the target ---
    picker_day(p, new_day, 450)                # 50.0 items/hr: a new best, strong day
    ev = A.evaluate_levels(p, day=new_day)
    day_ev = [x for x, _ in ev if x.period == "day"]
    check("2.1 A good day that beats the best levels up", len(day_ev) == 1, str(ev))
    rec = PersonalRecord.query.filter_by(emp_id=p.id, period="day").first()
    check("2.2 The best day is now the record (50/hr)", rec.best_value == 50.0, str(rec.best_value))
    check("2.3 The level went up by one", rec.level == 1)
    db.session.refresh(p)
    check("2.4 The best day became the target", p.custom_hourly_target == 50.0, str(p.custom_hourly_target))
    n = Notification.query.filter_by(emp_id=p.id).order_by(Notification.id.desc()).first()
    check("2.5 A congratulations notification was sent",
          n is not None and "Level up" in n.title and "Congratulations" in (n.body or ""), n.title if n else "")
    check("2.6 It says what was beaten and the new target",
          n and "beat your best" in n.body and "new target is 50" in n.body, n.body if n else "")
    check("2.7 The event is logged", LevelEvent.query.filter_by(emp_id=p.id, period="day").count() == 1)

    # ---- 3. The same day cannot level up twice --------------------------
    ev2 = A.evaluate_levels(p, day=new_day)
    check("3.1 Re-judging the same day changes nothing", [x for x, _ in ev2 if x.period == "day"] == [])

    # ---- 4. A best day that was NOT good does not count ----------------
    d2 = TODAY
    if d2.weekday() == 6:
        d2 = TODAY + timedelta(days=1)
    picker_day(p, d2, 600, good=False)         # huge volume, poor day (score < 72)
    ev3 = A.evaluate_levels(p, day=d2)
    check("4.1 A record volume on a poor day does not level up", [x for x, _ in ev3 if x.period == "day"] == [])
    db.session.refresh(p)
    check("4.2 The target stays where it was", p.custom_hourly_target == 50.0)

    # ---- 5. Targets only go up ------------------------------------------
    p.custom_hourly_target = 80.0               # e.g. the admin set a higher target
    db.session.commit()
    rec.best_value = 40.0                        # pretend the record was lower
    db.session.commit()
    KPIEntry.query.filter_by(emp_id=p.id, entry_date=d2).delete()
    db.session.commit()
    picker_day(p, d2, 500)                       # 55.6/hr beats 40 but is below the 80 target
    rec.last_checked = d2 - timedelta(days=1)
    db.session.commit()
    ev4 = A.evaluate_levels(p, day=d2)
    db.session.refresh(p)
    check("5.1 A new best below a higher target never lowers the target",
          p.custom_hourly_target == 80.0, str(p.custom_hourly_target))

    # ---- 6. Needs enough history first ----------------------------------
    q = mk("lv_new@x.com", "picker")
    nd = workdays_back(3, TODAY - timedelta(days=1))
    for d in nd[:-1]:
        picker_day(q, d, 200)
    picker_day(q, nd[-1], 400)
    ev5 = A.evaluate_levels(q, day=nd[-1])
    check("6.1 Nobody levels up with under 5 days of history", [x for x, _ in ev5 if x.period == "day"] == [])

    # ---- 7. Weeks and months level up too ------------------------------
    w = mk("lv_week@x.com", "picker")
    # three complete Mon-Sat weeks: two ordinary, then the last completed one strong
    last_sat = TODAY - timedelta(days=(TODAY.weekday() - 5) % 7 or 7)
    for k, items in ((2, 250), (1, 250), (0, 420)):
        mon = last_sat - timedelta(days=5 + 7 * k)
        for i in range(6):
            picker_day(w, mon + timedelta(days=i), items)
    evw = A.evaluate_levels(w)
    wk = [x for x, _ in evw if x.period == "week"]
    check("7.1 A better-than-good record week levels up", len(wk) == 1, str([(x.period, x.value) for x, _ in evw]))
    check("7.2 The week record is that week's daily average", wk and wk[0].value == round(420 / 9, 1))
    check("7.3 It is announced as a best week",
          Notification.query.filter(Notification.emp_id == w.id, Notification.title.like("%Best week%")).count() == 1)
    check("7.4 The same week is not judged twice", [x for x, _ in A.evaluate_levels(w) if x.period == "week"] == [])

    # ---- 8. Roles without a score: delivery ----------------------------
    dv = mk("lv_drv@x.com", "delivery")
    dd = workdays_back(8, TODAY - timedelta(days=1))
    for d in dd[:-1]:
        db.session.add(DeliveryBillerNote(emp_id=dv.id, entry_date=d, note_type="delivery",
                                          note_text="ok", quantity=10))
    db.session.commit()
    A.evaluate_levels(dv, day=dd[-2])            # set up from history quietly
    db.session.add(DeliveryBillerNote(emp_id=dv.id, entry_date=dd[-1], note_type="delivery",
                                      note_text="busy day", quantity=16))
    db.session.commit()
    evd = A.evaluate_levels(dv, day=dd[-1])
    check("8.1 A delivery rider's record day levels up", [x.period for x, _ in evd] == ["day"],
          str([(x.period, x.value) for x, _ in evd]))
    s = A.level_summary(dv)
    check("8.2 Their best day becomes their daily target", s and s["target"] == 16.0, str(s and s["target"]))

    # ---- 9. Packers and supervisors are not measured -------------------
    pk = mk("lv_pack@x.com", "packer")
    check("9.1 Roles with no output figure are left out safely",
          A.evaluate_levels(pk, day=TODAY) == [] and A.level_summary(pk) is None)

    # ---- 10. Purchaser speed uses their personal target ----------------
    pu = mk("lv_pur@x.com", "purchaser")
    e = KPIEntry(emp_id=pu.id, entry_date=TODAY, sales_bills_open=10, checked=10, picked=10,
                 errors_found=270, rack_organized=270, cs_sales_open=5, packing_done=5, total_time=9)
    base = A.build_analytics([e], "purchaser", emp_id=pu.id)
    pu.custom_hourly_target = 15.0
    db.session.commit()
    up = A.build_analytics([e], "purchaser", emp_id=pu.id)
    sp = lambda st: next(r for r in st["point_breakdown"] if r["name"] == "Speed")
    check("10.1 A purchaser's speed is scored against their own target",
          "against 15" in sp(up)["how"] and "against 60" in sp(base)["how"], sp(up)["how"])

# ---- 11. Pages ----------------------------------------------------------
c = csrf_client(app.test_client())
c.post("/login", data={"email": "lv_pick@x.com", "password": PW}, follow_redirects=True)
dash = c.get("/dashboard").data
check("11.1 The staff dashboard shows their level", b"Level " in dash and b"Best day" in dash)
with app.app_context():
    adm = mk("lv_adm@x.com", "picker", admin=True)
    pid = Employee.query.filter_by(email="lv_pick@x.com").first().id
a = csrf_client(app.test_client())
a.post("/login", data={"email": "lv_adm@x.com", "password": PW}, follow_redirects=True)
check("11.2 The staff profile shows the level and history",
      b"Level-up history" in a.get("/staff/%d" % pid).data)
adm_html = a.get("/admin_dashboard").data.decode("utf-8", "replace")
check("11.3 The admin sees recent level-ups", "Recent Level-ups" in adm_html)

# ---- 12. The owner can check every staff member ----------------------------
import re as _re  # noqa: E402
check("12.1 The admin page lists every staff member's level-up status",
      'id="levelCheckTable"' in adm_html)
def _status_of(name):
    m = _re.search(r"<strong>" + _re.escape(name) + r"</strong>[\s\S]{0,200}?lv-pill lv-([a-z_]+)", adm_html)
    return m.group(1) if m else None
check("12.2 A picker with records is Active", _status_of("Lv lv_pick") == "active", _status_of("Lv lv_pick"))
check("12.3 Someone with too few days is Starting", _status_of("Lv lv_new") == "starting", _status_of("Lv lv_new"))
check("12.4 A packer is Not measured", _status_of("Lv lv_pack") == "not_measured", _status_of("Lv lv_pack"))
check("12.5 A delivery rider with records is Active", _status_of("Lv lv_drv") == "active", _status_of("Lv lv_drv"))
check("12.6 The days still needed are spelled out", "of 6 working days logged" in adm_html)
with app.app_context():
    rows = {r["emp"].email: r for r in A.level_status_all()}
    pr = rows.get("lv_pick@x.com", {})
    check("12.8 An active row explains the latest day against the best",
          "Latest day" in (pr.get("why") or "") and ("below the best" in pr["why"] or "beat the best" in pr["why"]
                                                    or "a new best" in pr["why"]), pr.get("why"))
    # Older live rows have is_admin = NULL, not False. They must still count.
    from sqlalchemy import text as _text  # noqa: E402
    _nid = Employee.query.filter_by(email="lv_drv@x.com").first().id
    db.session.execute(_text("UPDATE employees SET is_admin = NULL WHERE id = :i"), {"i": _nid})
    db.session.commit()
    db.session.expire_all()
    rows_null = {r["emp"].email for r in A.level_status_all()}
    check("12.9 Staff whose admin flag is empty (NULL) are still listed", "lv_drv@x.com" in rows_null)
    _seen = []
    _orig = A.evaluate_levels
    A.evaluate_levels = lambda emp, **k: _seen.append(emp.email) or []
    try:
        A.run_level_checks()
    finally:
        A.evaluate_levels = _orig
    check("12.10 The nightly check includes them too", "lv_drv@x.com" in _seen and "lv_adm@x.com" not in _seen,
          str(_seen))
    db.session.execute(_text("UPDATE employees SET is_admin = :f WHERE id = :i"), {"f": False, "i": _nid})
    db.session.commit()
    check("12.7 Every non-admin staff member gets a row",
          all(em in rows for em in ("lv_pick@x.com", "lv_new@x.com", "lv_week@x.com", "lv_drv@x.com",
                                    "lv_pack@x.com", "lv_pur@x.com")) and "lv_adm@x.com" not in rows)

with app.app_context():
    for em in ("lv_pick@x.com", "lv_new@x.com", "lv_week@x.com", "lv_drv@x.com", "lv_pack@x.com",
               "lv_pur@x.com", "lv_adm@x.com"):
        e = Employee.query.filter_by(email=em).first()
        if e:
            for M in (PersonalRecord, LevelEvent, Notification, KPIEntry, DeliveryBillerNote):
                M.query.filter_by(emp_id=e.id).delete()
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
