"""Recommend hourly targets from this warehouse's real history.

A target nobody can reach makes those points dead weight and flattens every
staff member onto the same grade. A target everybody clears makes them free.
This reads the actual KPI history and proposes a value that a strong performer
reaches on a good month and an average one has to stretch for.

Method: take each person's items-per-hour over the window, then set the target
at the 75th percentile of those rates. Three quarters of the team sit below it,
so it is demanding; the strongest are at or above it, so it is reachable.

Read-only. It changes nothing -- it prints the numbers and the exact env vars to
set, plus what every person's score becomes if you apply them.

Usage:
    python3.13 tools_recommend_targets.py [days]      # default 90
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from datetime import date, timedelta  # noqa: E402

import app as A  # noqa: E402

WINDOW = int(sys.argv[1]) if len(sys.argv) > 1 else 90
PCTL = 0.75


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    i = p * (len(sorted_vals) - 1)
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def rate_for(emp, entries, role):
    """Items per hour over the window, or None when there is nothing to measure."""
    hours = sum(float(e.total_time or 0) for e in entries)
    if hours <= 0:
        return None
    if role == "picker":
        items = sum((e.picked or 0) + (e.missed or 0) for e in entries)
    else:
        items = sum(e.checked or 0 for e in entries)
    return (items / hours) if items > 0 else None


with A.app.app_context():
    since = date.today() - timedelta(days=WINDOW)
    print("Window: last %d days (from %s)\n" % (WINDOW, since.isoformat()))

    rows = {"picker": [], "checker": []}
    for emp in A.Employee.query.filter_by(is_admin=False).all():
        role = emp.staff_type
        if role not in ("picker", "checker"):
            sec = emp.secondary_staff_type or ""
            if sec not in ("picker", "checker"):
                continue
            role = sec
        ents = [e for e in emp.entries if e.entry_date and e.entry_date >= since]
        if not ents:
            continue
        r = rate_for(emp, ents, role)
        if r:
            rows[role].append((emp.name, round(r, 1), len(ents)))

    env_lines = []
    for role, var, current in (("picker", "PICKER_TARGET_HR", A.DEFAULT_PICKER_TARGET_HR),
                               ("checker", "CHECKER_TARGET_HR", A.DEFAULT_CHECKER_TARGET_HR)):
        data = sorted(rows[role], key=lambda t: -t[1])
        print("=" * 64)
        print("%s  (current target %.0f items/hr)" % (role.upper(), current))
        print("=" * 64)
        if not data:
            print("  No history in this window. Target left unchanged.\n")
            continue
        for name, r, n in data:
            print("  %-22s %6.1f items/hr   (%d days)" % (name, r, n))
        vals = sorted(r for _n, r, _d in data)
        p75 = pct(vals, PCTL)
        best = vals[-1]
        median = pct(vals, 0.5)
        target = round(p75)
        print("  %s" % ("-" * 56))
        print("  median %.1f | 75th pctl %.1f | best %.1f" % (median, p75, best))
        print("  RECOMMENDED: %d items/hr" % target)
        if current > best:
            print("  NOTE: the current target of %.0f is above the best anyone has ever"
                  % current)
            print("        achieved (%.1f), so these points are unreachable today." % best)
        env_lines.append("%s=%d" % (var, target))
        print()

    if not env_lines:
        print("Nothing to recommend.")
        sys.exit(0)

    print("=" * 64)
    print("TO APPLY — set these and redeploy:")
    print("=" * 64)
    for line in env_lines:
        print("  " + line)

    # Show the effect before anything is changed.
    print("\n" + "=" * 64)
    print("EFFECT ON SCORES (current -> recommended)")
    print("=" * 64)
    month_start = date.today().replace(day=1)
    before = {}
    for emp in A.Employee.query.filter_by(is_admin=False).order_by(A.Employee.name).all():
        ents = [e for e in emp.entries if e.entry_date and e.entry_date >= month_start]
        if not ents:
            continue
        s = A.build_analytics(ents, emp.staff_type, emp_id=emp.id)
        if s:
            before[emp.id] = (emp.name, s["eff_score"], s["grade"])

    for line in env_lines:
        k, v = line.split("=")
        if k == "PICKER_TARGET_HR":
            A.DEFAULT_PICKER_TARGET_HR = float(v)
        else:
            A.DEFAULT_CHECKER_TARGET_HR = float(v)

    changed = 0
    for emp in A.Employee.query.filter_by(is_admin=False).order_by(A.Employee.name).all():
        if emp.id not in before:
            continue
        ents = [e for e in emp.entries if e.entry_date and e.entry_date >= month_start]
        s = A.build_analytics(ents, emp.staff_type, emp_id=emp.id)
        name, old_score, old_grade = before[emp.id]
        new_score, new_grade = s["eff_score"], s["grade"]
        flag = ""
        if old_grade != new_grade:
            flag = "   grade %s -> %s" % (old_grade, new_grade)
            changed += 1
        print("  %-22s %5.1f -> %5.1f%s" % (name, old_score, new_score, flag))
    print("\n  %d staff change grade." % changed)
    print("\nNothing was written. Set the env vars above to apply.")
