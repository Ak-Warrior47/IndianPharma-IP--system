"""Adversarial checks on bill-based picking: ways a picker could cheat or the
rules could misfire, each one closed. Found by a strict review:
  * bill number 0 / 000000 was accepted
  * reporting every item "not found" in normal time paid full points for
    nothing picked (now: paid only once the assigner confirms Not available)
  * pickers who were not at work were warned as idle
  * the late report did not say how late
  * a double tap could start two grabs

Run:  python3.13 tests_picker_attack.py
"""
import os, sys
from datetime import datetime, timedelta, date
os.environ.pop("DATABASE_URL", None); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A
from app import app, db, Employee, PickBill, PickClaim, PickMissing, PickEvent, Notification
R = []
def ok(n, c, x=""): R.append(c); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "   " + str(x)))
with app.app_context():
    db.create_all()
    for M in (PickMissing, PickEvent): M.query.delete()
    PickClaim.query.delete(); PickBill.query.delete(); db.session.commit()
    def emp(em, st):
        e = Employee.query.filter_by(email=em).first()
        if not e:
            e = Employee(name=em.split("@")[0], email=em, staff_type=st); e.set_password("x" * 10); db.session.add(e); db.session.commit()
        Notification.query.filter_by(emp_id=e.id).delete(); db.session.commit()
        return e.id
    a = emp("atk_asg@x.com", "pick_assigner"); p1 = emp("atk_p1@x.com", "picker"); p2 = emp("atk_p2@x.com", "picker")
    absent = emp("atk_absent@x.com", "picker")
    # 1. bill number edge cases
    for bad in ["0", "000000", "", "U", "-5", "1234567", "45U6", "12a4"]:
        try: A.add_pick_bill(bad, 3, a); ok("1. bill number %r refused" % bad, False, "accepted")
        except A.PickError: ok("1. bill number %r refused" % bad, True)
    b = A.add_pick_bill(" 77u ", 4, a)
    ok("1. spaces and lower-case u are accepted as urgent", b.bill_no == A.fy_prefix() + "77" and b.is_urgent)
    # 2. exploit: report everything missing in normal time for full points without picking
    c = A.claim_items(p1, b.id, 4)
    c, _ = A.finish_claim(p1, c.id, missing=[("Ghost", 4)])
    ok("2. reporting ALL items missing earns no free points", (c.points or 0) <= 0, "points=%s" % c.points)
    mid = PickMissing.query.filter_by(claim_id=c.id).first().id
    A.handle_missing(mid, "unavailable")
    ok("2b. once the assigner confirms them as not available, the reporter is paid (1.5 x 4 = 6)",
       db.session.get(PickClaim, c.id).points == 6.0, db.session.get(PickClaim, c.id).points)
    # 3. picked + missing must match
    b2 = A.add_pick_bill("78", 5, a)
    c2 = A.claim_items(p2, b2.id, 3)
    try: A.finish_claim(p2, c2.id, missing=[("X", 5)]); ok("3. missing more than taken refused", False)
    except A.PickError: ok("3. missing more than taken refused", True)
    try: A.finish_claim(p2, c2.id, missing=[("", 1)]); ok("3. blank item name refused", False)
    except A.PickError: ok("3. blank item name refused", True)
    try: A.finish_claim(p1, c2.id); ok("3. another picker cannot finish my grab", False)
    except A.PickError: ok("3. another picker cannot finish my grab", True)
    A.finish_claim(p2, c2.id)
    # 4. idle warnings should not hit absent staff and should not repeat per bill
    for pid in (p1, p2):      # these two have the picking screen open, free for 6 minutes
        db.session.get(Employee, pid).last_seen_at = datetime.utcnow()
        for cl in PickClaim.query.filter_by(picker_id=pid).all():
            if cl.done_at: cl.done_at = datetime.utcnow() - timedelta(minutes=6)
    db.session.commit()
    for i in range(3):
        db.session.add(PickBill(bill_no="IDX%d" % i, items=2, created_at=datetime.utcnow() - timedelta(minutes=10)))
    db.session.commit()
    A.alert_late_picks(); A.alert_late_picks(datetime.utcnow() + timedelta(minutes=1))
    ok("4. a picker who never logged in today is not warned as idle",
       PickEvent.query.filter_by(kind="idle", picker_id=absent).count() == 0,
       PickEvent.query.filter_by(kind="idle", picker_id=absent).count())
    ok("4. a picker on the picking screen IS warned", PickEvent.query.filter_by(kind="idle", picker_id=p1).count() == 1)
    n_idle = PickEvent.query.filter_by(kind="idle", picker_id=p1).count()
    ok("4. one idle stretch gives one warning, not one per waiting bill", n_idle <= 1, n_idle)
    # 5. late event records how late
    b5 = A.add_pick_bill("79", 1, a)
    c5 = A.claim_items(p1, b5.id, 1, now=datetime.utcnow() - timedelta(minutes=8))
    A.alert_late_picks()
    c5, _ = A.finish_claim(p1, c5.id)
    ev = PickEvent.query.filter_by(kind="late", picker_id=p1, bill_id=b5.id).first()
    ok("5. the late report says how many minutes late", ev is not None and (ev.minutes or 0) > 5, ev and ev.minutes)
    # 6. double take by the same picker on two bills
    b6a, b6b = A.add_pick_bill("80", 2, a), A.add_pick_bill("81", 2, a)
    A.claim_items(p2, b6a.id, 1)
    try: A.claim_items(p2, b6b.id, 1); ok("6. second grab while one is active refused", False)
    except A.PickError: ok("6. second grab while one is active refused", True)
    # cleanup
    for M in (PickMissing, PickEvent): M.query.delete()
    PickClaim.query.delete(); PickBill.query.delete()
    for em in ("atk_asg@x.com", "atk_p1@x.com", "atk_p2@x.com", "atk_absent@x.com"):
        e = Employee.query.filter_by(email=em).first()
        Notification.query.filter_by(emp_id=e.id).delete(); db.session.delete(e)
    db.session.commit()
print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (sum(R), len(R) - sum(R)))
print("=" * 62)
sys.exit(0 if all(R) else 1)
