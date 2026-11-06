"""
Pharma IP System — KRA performance report (PDF).

Every number in this report comes from build_analytics() in app.py: the points
table is the `point_breakdown` the scoring code returns, pending and clearance
are the checking TEAM's figures, and urgent work is counted at its weight. The
report used to carry its own copy of the formulas, which drifted -- it still
divided speed by 80, took clearance from a hand-typed pending, treated urgent
work as "errors", and printed a section for a feature that had been removed.
There is deliberately no formula here that the app does not also use.

Layout: one summary page (score, how it was reached, points table), then the
figures by period with the work mix, multitask and deductions, then the daily
log and a short formula reference. A footer on every page.
"""

import io
import logging
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# ── A restrained palette: one brand colour, greys, and status colours used
#    only where a value is good or bad. ─────────────────────────────────────
INK = colors.HexColor("#0f172a")
INK_2 = colors.HexColor("#334155")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#e2e8f0")
ZEBRA = colors.HexColor("#f8fafc")
HEAD = colors.HexColor("#1e293b")
BRAND = colors.HexColor("#1d4ed8")
BRAND_LT = colors.HexColor("#dbeafe")
OK = colors.HexColor("#15803d")
WARN = colors.HexColor("#b45309")
BAD = colors.HexColor("#b91c1c")
URGENT = colors.HexColor("#c2410c")

GRADE_COLOR = {"ELITE": colors.HexColor("#6d28d9"), "PROFICIENT": OK,
               "SATISFACTORY": WARN, "RE-TRAINING": BAD, "NOT SCORED": MUTED}

PAGE_W, PAGE_H = A4
MARGIN = 1.6 * cm
CONTENT_W = PAGE_W - 2 * MARGIN


def _st(name, **kw):
    base = dict(fontName="Helvetica", fontSize=9, leading=12, textColor=INK_2)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "title": _st("t", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK),
    "sub": _st("s", fontSize=9, textColor=MUTED),
    "h2": _st("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=INK,
              spaceBefore=14, spaceAfter=6),
    "body": _st("b"),
    "small": _st("sm", fontSize=7.5, leading=10, textColor=MUTED),
    "cell": _st("c", fontSize=8, leading=10),
    "cellb": _st("cb", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=INK),
    "cellr": _st("cr", fontSize=8, leading=10, alignment=TA_RIGHT),
    "head": _st("hd", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.white),
    "headr": _st("hr", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.white,
                 alignment=TA_RIGHT),
}


def _g(stats, key, default=0):
    try:
        v = (stats or {}).get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _num(v, dp=0, suffix=""):
    """Format a number for print: no 8.09999999 floats, thousands separated."""
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = ("{:,.%df}" % dp).format(f)
    return s + suffix


def _has(stats):
    return bool(stats) and _g(stats, "days", 0) > 0


def _table(rows, widths, head=True, zebra=True, align_right_from=1, bold_last=False):
    """A plain, consistent table: dark header, hairlines, light zebra rows."""
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK_2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
    ]
    if align_right_from is not None:
        style.append(("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"))
    if head:
        style += [("BACKGROUND", (0, 0), (-1, 0), HEAD),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5)]
    if zebra:
        for r in range(1 if head else 0, len(rows)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
    if bold_last:
        style += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5),
                  ("TEXTCOLOR", (0, -1), (-1, -1), INK),
                  ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK)]
    t.setStyle(TableStyle(style))
    return t


def _bar(got, cap, w=3.2 * cm, h=5):
    """A small progress bar for the points table."""
    d = Drawing(w, h + 2)
    d.add(Rect(0, 1, w, h, fillColor=colors.HexColor("#eef2f7"), strokeColor=None))
    frac = max(0.0, min(float(got) / float(cap), 1.0)) if cap else 0
    if frac > 0:
        d.add(Rect(0, 1, w * frac, h, fillColor=BRAND, strokeColor=None))
    return d


def _pie(title, parts, size=3.4 * cm):
    """A pie with the legend BESIDE it (never on top of the slices)."""
    total = sum(v for _, v, _ in parts)
    w, h = CONTENT_W / 2 - 0.3 * cm, size + 0.9 * cm
    d = Drawing(w, h)
    d.add(String(0, h - 10, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    if total <= 0:
        d.add(String(0, h - 26, "No figures for this period", fontName="Helvetica",
                     fontSize=8, fillColor=MUTED))
        return d
    p = Pie()
    p.x, p.y, p.width, p.height = 0, 4, size, size
    p.data = [max(v, 0.0001) for _, v, _ in parts]
    p.labels = None
    p.slices.strokeColor = colors.white
    p.slices.strokeWidth = 1.2
    for i, (_, _, c) in enumerate(parts):
        p.slices[i].fillColor = c
    d.add(p)
    lx, ly = size + 0.5 * cm, size - 6
    for label, v, c in parts:
        d.add(Rect(lx, ly - 1, 7, 7, fillColor=c, strokeColor=None))
        d.add(String(lx + 11, ly, label, fontName="Helvetica", fontSize=8, fillColor=INK_2))
        d.add(String(lx + 11, ly - 10, "%s  ·  %s%%" % (_num(v), _num(v / total * 100, 1)),
                     fontName="Helvetica-Bold", fontSize=8, fillColor=INK))
        ly -= 26
    return d


# ── role-specific figure rows: (label, key-or-function, decimals, suffix) ──
def _checker_rows():
    return [
        ("Bills checked — normal", "tsb_normal", 0, ""),
        ("Bills checked — urgent", "tsb_urgent", 0, ""),
        ("Items checked — normal", "tck_normal", 0, ""),
        ("Items checked — urgent", "tck_urgent", 0, ""),
        ("Weighted items (urgent × 1.5)", "weighted_items", 0, ""),
        ("Weighted speed / hr", "check_speed_weighted", 1, ""),
        ("Team: day's total bills", "team_total_bills", 0, ""),
        ("Team: bills checked", "team_checked_bills", 0, ""),
        ("Team: pending bills", "team_pending_bills", 0, ""),
        ("Team: clearance", lambda s: _g(s, "clearance_rate") if _g(s, "team_total_bills") else None, 1, "%"),
        ("Own share of team's work", "team_share_pct", 1, "%"),
        ("Consistency", "consistency", 1, "%"),
    ]


def _picker_rows():
    return [
        ("Bills given to pick", "tbr_picker", 0, ""),
        ("Bills picked", "tsb_normal", 0, ""),
        ("Bill fulfilment", "bill_fulfilment", 1, "%"),
        ("Items picked", "tp", 0, ""),
        ("Items missed", "tm", 0, ""),
        ("Pick accuracy", "pick_acc", 1, "%"),
        ("Pick speed / hr", "pick_speed", 1, ""),
        ("CS fulfilment", "cs_fulfilment", 1, "%"),
        ("Workspace", "workspace_score", 1, "%"),
        ("Consistency", "consistency", 1, "%"),
    ]


def _purchaser_rows():
    return [
        ("PO bills received", "pur_bills_received", 0, ""),
        ("PO bills checked", "pur_bills_checked", 0, ""),
        ("Bill processing", "pur_bill_rate", 1, "%"),
        ("PO bill entry", "pur_bill_entry", 0, ""),
        ("Items", "pur_items", 0, ""),
        ("Items racked", "pur_items_racked", 0, ""),
        ("Racking", "pur_racking_eff", 1, "%"),
        ("CS fulfilment", "pur_cs_fulfilment", 1, "%"),
        ("Pending PO bills", "pur_pending_bills", 0, ""),
        ("Speed / hr", "pur_speed", 1, ""),
        ("Consistency", "consistency", 1, "%"),
    ]


def _val(stats, key):
    if not _has(stats):
        return None
    return key(stats) if callable(key) else _g(stats, key, None)


# ── the report ─────────────────────────────────────────────────────────────
def generate_visual_pdf(emp_name, payload):
    payload = payload or {}
    st_type = (payload.get("staff_type") or "").lower()
    week, month, alls = payload.get("week_stats"), payload.get("month_stats"), payload.get("all_stats")
    today = date.today()

    # The headline is THIS MONTH -- the period a KRA is judged on. If nothing
    # was entered this month yet, fall back to all time and say so.
    head_stats, head_label = (month, today.strftime("%B %Y")) if _has(month) else (alls, "All time")
    hs = head_stats or {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=1.4 * cm, bottomMargin=1.6 * cm,
                            title="KRA Report — %s" % emp_name, author="Pharma IP System")

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, 1.1 * cm, PAGE_W - MARGIN, 1.1 * cm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 0.75 * cm, "Pharma IP System  ·  KRA report for %s  ·  Generated %s  ·  Confidential"
                          % (emp_name, today.strftime("%d %b %Y")))
        canvas.drawRightString(PAGE_W - MARGIN, 0.75 * cm, "Page %d" % doc_.page)
        canvas.restoreState()

    E = []
    try:
        # ── Header ──────────────────────────────────────────────────────
        role = (payload.get("emp_role") or st_type or "").title()
        rank_bits = []
        if payload.get("emp_role_rank") and payload.get("total_role"):
            rank_bits.append("#%s of %s %ss" % (payload["emp_role_rank"], payload["total_role"], st_type or "staff"))
        if payload.get("emp_overall_rank") and payload.get("total_staff"):
            rank_bits.append("#%s of %s overall" % (payload["emp_overall_rank"], payload["total_staff"]))
        E += [Paragraph("KRA Performance Report", S["sub"]),
              Paragraph(emp_name, S["title"]),
              Paragraph("%s  ·  Assessed period: <b>%s</b>%s" % (
                  role, head_label, ("  ·  " + "  ·  ".join(rank_bits)) if rank_bits else ""), S["sub"]),
              Spacer(1, 0.35 * cm)]

        # ── Score summary tiles ─────────────────────────────────────────
        grade = _g(hs, "grade", "NO DATA")
        gcol = GRADE_COLOR.get(grade, MUTED)

        def tile(label, value, sub, vcolor=INK):
            return [Paragraph(label.upper(), _st("tl", fontName="Helvetica-Bold", fontSize=6.5, textColor=MUTED)),
                    Paragraph(value, _st("tv", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=vcolor)),
                    Paragraph(sub, S["small"])]
        tiles = [
            tile("Score — " + head_label, "%s<font size=9 color='#64748b'> / 100</font>" % _num(_g(hs, "eff_score"), 1),
                 "Grade: <font color='%s'><b>%s</b></font>" % (gcol.hexval().replace("0x", "#"), grade)),
            tile("This week", _num(_g(week, "eff_score"), 1) if _has(week) else "—",
                 "%s day(s) entered" % _g(week, "days", 0)),
            tile("All time", _num(_g(alls, "eff_score"), 1) if _has(alls) else "—",
                 "%s day(s) entered" % _g(alls, "days", 0)),
        ]
        tt = Table([tiles], colWidths=[CONTENT_W * 0.40, CONTENT_W * 0.30, CONTENT_W * 0.30])
        tt.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEAFTER", (0, 0), (-2, -1), 0.6, LINE),
            ("BACKGROUND", (0, 0), (0, 0), BRAND_LT),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
        E.append(tt)

        # ── How the score was reached (reconciles every point) ──────────
        if _has(hs):
            E.append(Paragraph("How the %s score was reached" % head_label
                               if head_label != "All time" else "How the score was reached", S["h2"]))
            recon = [["", "Points"],
                     ["KPI points (table below)", _num(_g(hs, "base_score"), 1)]]
            if _g(hs, "multitask_bonus"):
                recon.append(["+ Multitask bonus  (%s pts over %s extra-role days, spread over %s days)"
                              % (_num(_g(hs, "multitask_day_points")), _g(hs, "multitask_days"), _g(hs, "days")),
                              "+" + _num(_g(hs, "multitask_bonus"), 1)])
            if _g(hs, "admin_adjustment"):
                a = float(_g(hs, "admin_adjustment"))
                recon.append(["%s Admin adjustment" % ("+" if a > 0 else "−"), ("+" if a > 0 else "") + _num(a, 1)])
            if _g(hs, "complaint_deduction"):
                recon.append(["− Complaint deductions", "−" + _num(_g(hs, "complaint_deduction"), 1)])
            if _g(hs, "supervisor_penalty"):
                recon.append(["− Supervisor count mismatches", "−" + _num(_g(hs, "supervisor_penalty"), 1)])
            recon.append(["Final score (capped at 100)", _num(_g(hs, "eff_score"), 1)])
            E.append(_table(recon, [CONTENT_W * 0.80, CONTENT_W * 0.20], bold_last=True))

            # ── Points table: straight from the scoring code ─────────────
            bd = _g(hs, "point_breakdown", []) or []
            if bd:
                E.append(Paragraph("KPI points, component by component", S["h2"]))
                rows = [[Paragraph("Component", S["head"]), "", Paragraph("Points", S["headr"]),
                         Paragraph("How it was worked out", S["head"])]]
                for r in bd:
                    rows.append([Paragraph(r["name"], S["cellb"]), _bar(r["got"], r["max"]),
                                 Paragraph("<b>%s</b> / %s" % (_num(r["got"], 1), r["max"]), S["cellr"]),
                                 Paragraph(r.get("how", ""), S["cell"])])
                rows.append([Paragraph("KPI points", S["cellb"]), "",
                             Paragraph("<b>%s</b> / 100" % _num(sum(r["got"] for r in bd), 1), S["cellr"]), ""])
                t = _table(rows, [3.6 * cm, 3.4 * cm, 2.0 * cm, CONTENT_W - 9.0 * cm],
                           align_right_from=None, bold_last=True)
                E.append(t)
        else:
            E += [Spacer(1, 0.4 * cm), Paragraph("No entries yet, so there is no score to report.", S["body"])]

        # ── Figures by period ───────────────────────────────────────────
        E.append(PageBreak())
        E.append(Paragraph("Figures by period", S["h2"]))
        defs = _checker_rows() if st_type == "checker" else (
            _purchaser_rows() if st_type == "purchaser" else _picker_rows())
        rows = [["", "This week", "This month", "All time"]]
        for label, key, dp, suf in defs:
            rows.append([label] + [_num(_val(s, key), dp, suf) if _val(s, key) is not None else "—"
                                   for s in (week, month, alls)])
        rows.append(["Score"] + [_num(_g(s, "eff_score"), 1) if _has(s) else "—" for s in (week, month, alls)])
        rows.append(["Grade"] + [_g(s, "grade", "—") if _has(s) else "—" for s in (week, month, alls)])
        E.append(_table(rows, [CONTENT_W * 0.40] + [CONTENT_W * 0.20] * 3))
        if st_type == "checker":
            note = ("Pending and clearance are the checking TEAM's figures: the day's total sales bills minus the "
                    "bills checked by all checkers. The same numbers for every checker on the day.")
            if _g(alls, "team_per_person_days"):
                note += (" On %s earlier day(s) each checker typed their own bills instead of the day's total; "
                         "those figures are added together." % _g(alls, "team_per_person_days"))
            E.append(Paragraph(note, S["small"]))

        # ── Work mix (one or two honest charts, legends beside) ─────────
        ms = head_stats or {}
        charts = []
        if st_type == "checker":
            charts.append(_pie("Items checked — %s" % head_label, [
                ("Normal", float(_g(ms, "tck_normal")), BRAND),
                ("Urgent (worth 1.5×)", float(_g(ms, "tck_urgent")), URGENT)]))
            charts.append(_pie("Team bills — %s" % head_label, [
                ("Checked", float(_g(ms, "team_checked_bills")), OK),
                ("Pending", float(_g(ms, "team_pending_bills")), colors.HexColor("#cbd5e1"))]))
        elif st_type == "picker":
            charts.append(_pie("Items — %s" % head_label, [
                ("Picked", float(_g(ms, "tp")), BRAND), ("Missed", float(_g(ms, "tm")), BAD)]))
            given, picked = float(_g(ms, "tbr_picker")), float(_g(ms, "tsb_normal"))
            charts.append(_pie("Bills — %s" % head_label, [
                ("Picked", min(picked, given) if given else picked, OK),
                ("Not picked", max(given - picked, 0), colors.HexColor("#cbd5e1"))]))
        elif st_type == "purchaser":
            charts.append(_pie("PO bills — %s" % head_label, [
                ("Checked", float(_g(ms, "pur_bills_checked")), OK),
                ("Pending", float(_g(ms, "pur_pending_bills")), colors.HexColor("#cbd5e1"))]))
        if charts:
            E.append(Paragraph("Work mix", S["h2"]))
            ct = Table([charts + ([""] if len(charts) == 1 else [])], colWidths=[CONTENT_W / 2] * 2)
            ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            E.append(ct)

        # ── Multitask ───────────────────────────────────────────────────
        mb = _g(ms, "multitask_breakdown", []) or []
        if mb:
            rows = [["Date", "Extra roles worked", "Rule", "Points"]]
            for d in mb[:20]:
                rows.append([d["date"].strftime("%d %b %Y"), ", ".join(r.title() for r in d["roles"]),
                             "%d × 10%s" % (len(d["roles"]), ", capped at 30" if d["raw"] > 30 else ""),
                             str(d["points"])])
            E.append(KeepTogether([
                Paragraph("Multitask — %s" % head_label, S["h2"]),
                Paragraph("Each extra role on a day earns 10 points, up to 30 that day. The day points are spread "
                          "over every day in the period: %s ÷ %s days = +%s."
                          % (_num(_g(ms, "multitask_day_points")), _g(ms, "days"), _num(_g(ms, "multitask_bonus"), 1)),
                          S["small"]),
                Spacer(1, 4),
                _table(rows, [3 * cm, CONTENT_W - 8.5 * cm, 3.5 * cm, 2 * cm], align_right_from=3)]))

        # ── Complaints that reduced the score ───────────────────────────
        comps = payload.get("complaints_targeted") or []
        if comps:
            key = {"checker": "checker_pts", "purchaser": "purchaser_pts"}.get(st_type, "picker_pts")
            rows = [["Date", "Type", "Detail", "Points"]]
            for c in comps[:15]:
                d = c.get("date")
                rows.append([d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d or ""),
                             str(c.get("type") or ""), Paragraph(str(c.get("description") or ""), S["cell"]),
                             "−" + _num(c.get(key) or 0, 1)])
            E.append(KeepTogether([Paragraph("Complaints", S["h2"]),
                                   _table(rows, [2.6 * cm, 3 * cm, CONTENT_W - 7.6 * cm, 2 * cm],
                                          align_right_from=3)]))

        # ── Daily log ───────────────────────────────────────────────────
        entries = payload.get("all_entries") or []
        if entries:
            E.append(PageBreak())
            E.append(Paragraph("Daily entry log", S["h2"]))
            E.append(Paragraph("Most recent %d entries, newest first." % len(entries), S["small"]))
            E.append(Spacer(1, 4))
            if st_type == "checker":
                td = payload.get("team_days") or {}
                rows = [["Date", "Bills\nnormal", "Bills\nurgent", "Items\nnormal", "Items\nurgent",
                         "Day total\n(team)", "Team\nchecked", "Team\npending", "Team\nclearance"]]
                for e in entries:
                    t = td.get(e.entry_date) or {}
                    tot = t.get("total") or 0
                    mark = " *" if t.get("mode") == "per_person" else (" !" if t.get("mode") == "conflict" else "")
                    rows.append([e.entry_date.strftime("%d %b %Y"), _num(e.sales_bills_open or 0),
                                 _num(e.cs_sales_open or 0), _num(e.checked or 0), _num(e.errors_found or 0),
                                 _num(tot) + mark, _num(t.get("checked") or 0), _num(t.get("pending") or 0),
                                 _num(min(t.get("checked", 0) / tot * 100, 100), 1, "%") if tot else "—"])
                E.append(_table(rows, [2.4 * cm] + [(CONTENT_W - 2.4 * cm) / 8] * 8))
                E.append(Paragraph("* That day each checker typed their own bills, not the day's total; their "
                                   "figures are added together.  ! The figures entered that day do not add up.",
                                   S["small"]))
            elif st_type == "purchaser":
                rows = [["Date", "PO bills", "PO checked", "PO entry", "Items", "Racked", "CS open", "CS rcvd"]]
                for e in entries:
                    rows.append([e.entry_date.strftime("%d %b %Y")] + [_num(x or 0) for x in (
                        e.sales_bills_open, e.checked, e.picked, e.errors_found, e.rack_organized,
                        e.cs_sales_open, e.packing_done)])
                E.append(_table(rows, [2.6 * cm] + [(CONTENT_W - 2.6 * cm) / 7] * 7))
            else:
                rows = [["Date", "Bills given", "Bills picked", "Items picked", "Missed", "Accuracy", "Speed / hr"]]
                for e in entries:
                    tot = (e.picked or 0) + (e.missed or 0)
                    rows.append([e.entry_date.strftime("%d %b %Y"), _num(e.total_bills_received or 0),
                                 _num(e.sales_bills_open or 0), _num(e.picked or 0), _num(e.missed or 0),
                                 _num((e.picked or 0) / tot * 100, 1, "%") if tot else "—",
                                 _num(tot / 9.0, 1)])
                E.append(_table(rows, [2.6 * cm] + [(CONTENT_W - 2.6 * cm) / 6] * 6))

        # ── Formula reference: exactly what the app computes ────────────
        refs = ["Each component's points and working are listed on page 1 — they are the figures the app "
                "itself awarded.",
                "Final score = KPI points + multitask bonus ± admin adjustment − complaint deductions "
                "− supervisor mismatches, capped at 100.",
                "Multitask: 10 points per extra role per day, up to 30 a day, spread over every day in the period.",
                "Grades: ELITE 88+  ·  PROFICIENT 72+  ·  SATISFACTORY 52+  ·  RE-TRAINING below 52."]
        if st_type == "checker":
            refs[1:1] = [
                "Urgent work counts 1.5×: weighted bills = normal + urgent × 1.5; weighted items likewise.",
                "Team pending = the day's total sales bills − bills checked by ALL checkers (normal + urgent).",
                "Team clearance = bills checked by all checkers ÷ the day's total × 100.",
                "Normal + urgent bills can never exceed the day's total — for one checker or the team together."]
        E.append(KeepTogether([Paragraph("How the figures are calculated", S["h2"])] +
                              [Paragraph("•  " + r, S["small"]) for r in refs]))

        doc.build(E, onFirstPage=footer, onLaterPages=footer)
    except Exception:
        logger.exception("generate_visual_pdf failed")
        buf = io.BytesIO()
        SimpleDocTemplate(buf, pagesize=A4).build(
            [Paragraph("The report could not be generated. Please try again or contact the administrator.",
                       S["body"])])
    buf.seek(0)
    return buf
