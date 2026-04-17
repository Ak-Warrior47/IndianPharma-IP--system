"""
Pharma IP System — PDF report generator.
Produces a polished A4 KRA report per staff member with:
  • Header (grade, points, trend, complaint deduction)
  • Formula reference (aligned with the scoring in app.py)
  • Points breakdown — visual bars per component
  • KPI table (Today / Week / Month / All-Time)
  • Pie charts (volume / points earned vs remaining / role-specific)
  • Period trend bars
  • Daily entry log (role-aware columns, sorted newest first)

Keep formulas and grade thresholds in sync with `build_analytics` in app.py.
"""

import io
import logging
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.shapes import Drawing, String, Rect, Line
from reportlab.graphics.charts.piecharts import Pie

logger = logging.getLogger(__name__)

# ── Color palette — matches staff_detail.html ──────────────────────────
NAVY     = "#0a1628"
NAVY_2   = "#1a2942"
BRAND    = "#1e40af"
BRAND_2  = "#2563eb"
BRAND_LT = "#dbeafe"
EMERALD  = "#059669"
EM_LT    = "#d1fae5"
AMBER    = "#d97706"
AM_LT    = "#fef3c7"
ROSE     = "#dc2626"
RO_LT    = "#fee2e2"
GOLD     = "#b45309"
PURPLE   = "#6d28d9"
GRAY     = "#64748b"
GRAY_2   = "#94a3b8"
LGRAY    = "#e2e8f0"
WHITE    = "#ffffff"
SURFACE  = "#f7f9fc"
SURF_2   = "#f1f5fa"

GRADE_COLORS = {
    "ELITE":        PURPLE,
    "PROFICIENT":   EMERALD,
    "SATISFACTORY": AMBER,
    "RE-TRAINING":  ROSE,
}


# ── small helpers ──────────────────────────────────────────────────────
def _ps(S, name, **kw):
    return ParagraphStyle(name, parent=S["Normal"], **kw)


def _gv(stats, key, default=0):
    """Safe stat getter — returns default if stats is None or key missing."""
    if not stats:
        return default
    v = stats.get(key)
    return default if v is None else v


def _sv(stats, key, suffix=""):
    """Safe stat getter formatted for table cells."""
    if not stats:
        return "—"
    v = stats.get(key)
    return f"{v}{suffix}" if v is not None else "—"


# ── chart helpers ──────────────────────────────────────────────────────
def _make_pie(labels, vals, hex_colors, title, w=175, h=145):
    """Donut-style pie with colored legend swatches below."""
    try:
        d = Drawing(w, h)
        safe_vals = [max(float(v), 0.01) for v in vals]
        pie = Pie()
        pie.x, pie.y = 30, 24
        pie.width = pie.height = 88
        pie.data = safe_vals
        pie.simpleLabels = True
        pie.sideLabels   = False
        pie.labels       = None  # we draw our own legend
        pie.slices.strokeWidth = 1.2
        pie.slices.strokeColor = colors.HexColor(WHITE)
        for i, c in enumerate(hex_colors):
            if i < len(pie.slices):
                pie.slices[i].fillColor = colors.HexColor(c)
        d.add(pie)
        # Title
        d.add(String(w / 2, h - 9, title, fontSize=8, fontName="Helvetica-Bold",
                     fillColor=colors.HexColor(NAVY), textAnchor="middle"))
        # Legend
        for i, (lbl, v) in enumerate(zip(labels, vals)):
            y = h - 28 - i * 13
            d.add(Rect(4, y - 2, 9, 9,
                       fillColor=colors.HexColor(hex_colors[i]), strokeColor=None))
            d.add(String(17, y + 3, f"{lbl}: {v}", fontSize=7,
                         fillColor=colors.HexColor(GRAY), textAnchor="start"))
        return d
    except Exception as e:
        logger.error(f"Pie error: {e}")
        d2 = Drawing(w, h)
        d2.add(String(w / 2, h / 2, "Chart error", fontSize=8,
                      textAnchor="middle", fillColor=colors.HexColor(GRAY)))
        return d2


def _make_bars(vals, labels, title, w=350, h=110, max_val=100):
    """Vertical bar chart. Bars for 0-values render as a faint baseline tick, not a fake bar."""
    try:
        d = Drawing(w, h)
        d.add(String(w / 2, h - 8, title, fontSize=8, fontName="Helvetica-Bold",
                     fillColor=colors.HexColor(NAVY), textAnchor="middle"))
        bar_w  = 52
        gap    = 16
        x0     = 28
        area_h = h - 40
        baseline_y = 22
        # baseline
        d.add(Line(x0 - 4, baseline_y, w - 4, baseline_y,
                   strokeColor=colors.HexColor(LGRAY), strokeWidth=0.5))
        shades = [BRAND, BRAND_2, "#3b82f6", "#60a5fa"]
        for i, (v, lbl) in enumerate(zip(vals, labels)):
            sv = max(float(v) if v else 0, 0)
            x  = x0 + i * (bar_w + gap)
            if sv <= 0:
                d.add(String(x + bar_w / 2, baseline_y + 8, "—",
                             fontSize=8, fillColor=colors.HexColor(GRAY_2),
                             textAnchor="middle"))
            else:
                bh = max(int(sv / max(float(max_val), 1) * area_h), 3)
                d.add(Rect(x, baseline_y, bar_w, bh,
                           fillColor=colors.HexColor(shades[min(i, 3)]),
                           strokeColor=None))
                d.add(String(x + bar_w / 2, baseline_y + bh + 3, str(round(sv, 1)),
                             fontSize=7, fontName="Helvetica-Bold",
                             fillColor=colors.HexColor(NAVY), textAnchor="middle"))
            d.add(String(x + bar_w / 2, 8, lbl, fontSize=7,
                         fillColor=colors.HexColor(GRAY), textAnchor="middle"))
        return d
    except Exception as e:
        logger.error(f"Bar error: {e}")
        d2 = Drawing(w, h)
        d2.add(String(w / 2, h / 2, "Chart error", fontSize=8,
                      textAnchor="middle", fillColor=colors.HexColor(GRAY)))
        return d2


def _points_bar_row(label, earned, cap, width_cm=14):
    """Horizontal progress bar for one scoring component. Returns a Drawing."""
    w = width_cm * cm
    h = 18
    d = Drawing(w, h)
    cap_f = max(float(cap), 0.01)
    earned_f = max(min(float(earned), cap_f), 0)
    pct = earned_f / cap_f
    # track
    d.add(Rect(0, 4, w, 8, fillColor=colors.HexColor(SURF_2),
               strokeColor=colors.HexColor(LGRAY), strokeWidth=0.3))
    # fill
    if pct > 0:
        d.add(Rect(0, 4, w * pct, 8, fillColor=colors.HexColor(BRAND_2),
                   strokeColor=None))
    # label on the left (above bar)
    # (we place text separately via Paragraph — this just draws the bar)
    return d


# ── main entry point ──────────────────────────────────────────────────
def generate_visual_pdf(emp_name, payload):
    _buf = io.BytesIO()
    try:
        _doc = SimpleDocTemplate(
            _buf, pagesize=A4,
            rightMargin=1.6 * cm, leftMargin=1.6 * cm,
            topMargin=1.6 * cm,   bottomMargin=1.6 * cm,
            title=f"KRA Report — {emp_name}",
            author="Pharma IP System",
        )
        S = getSampleStyleSheet()

        def p(n, **kw):
            return _ps(S, n, **kw)

        # Typography
        title_st = p("T1", fontSize=18, fontName="Helvetica-Bold", alignment=TA_CENTER,
                     textColor=colors.HexColor(NAVY), spaceAfter=2, leading=22)
        sub_st   = p("T2", fontSize=9, alignment=TA_CENTER,
                     textColor=colors.HexColor(GRAY), spaceAfter=2, leading=12)
        h2_st    = p("H2", fontSize=11, fontName="Helvetica-Bold",
                     textColor=colors.HexColor(NAVY), spaceBefore=10, spaceAfter=6,
                     leading=14)
        body_st  = p("B1", fontSize=9, textColor=colors.HexColor(NAVY),
                     spaceAfter=4, leading=13)
        formula_st = p("FM", fontSize=7.8, textColor=colors.HexColor(GRAY),
                       fontName="Helvetica", spaceAfter=2, leading=11)
        pt_label_st = p("PL", fontSize=9, textColor=colors.HexColor(NAVY),
                        fontName="Helvetica-Bold", leading=11)
        pt_val_st   = p("PV", fontSize=9, textColor=colors.HexColor(GRAY),
                        alignment=TA_RIGHT, leading=11)

        # ── pull payload ─────────────────────────────────────────────
        all_s   = payload.get("all_stats")
        d_s     = payload.get("day_stats")
        w_s     = payload.get("week_stats")
        m_s     = payload.get("month_stats")
        stype   = payload.get("staff_type", "picker")
        entries = payload.get("all_entries", []) or []
        # Ensure entries are newest-first
        try:
            entries = sorted(entries, key=lambda e: e.entry_date, reverse=True)
        except Exception:
            pass

        elems = []

        # ── NO DATA CASE ─────────────────────────────────────────────
        if not all_s:
            elems += [
                Paragraph("PHARMA IP SYSTEM", title_st),
                Paragraph(f"KRA Performance Report — {emp_name}", sub_st),
                Spacer(1, 0.6 * cm),
                HRFlowable(width="100%", thickness=1, color=colors.HexColor(LGRAY)),
                Spacer(1, 0.6 * cm),
                Paragraph("No performance data available yet for this staff member.", body_st),
                Paragraph("Once they submit daily metrics, a full analytics report will appear here.",
                          p("NB", fontSize=9, textColor=colors.HexColor(GRAY))),
            ]
            _doc.build(elems)
            _buf.seek(0)
            return _buf

        # ── HEADER ───────────────────────────────────────────────────
        grade     = all_s.get("grade", "N/A")
        grade_clr = GRADE_COLORS.get(grade, NAVY)
        score     = all_s.get("eff_score", 0)
        deduction = all_s.get("complaint_deduction", 0) or 0

        # Big grade + score block
        grade_para = Paragraph(
            f"<font size='10' color='{GRAY}'>KRA GRADE</font><br/>"
            f"<font size='22' color='{grade_clr}'><b>{grade}</b></font>",
            p("GR", alignment=TA_LEFT, leading=26))
        score_para = Paragraph(
            f"<font size='10' color='{GRAY}'>POINTS SCORE</font><br/>"
            f"<font size='28' color='{NAVY}'><b>{score}</b></font>"
            f"<font size='13' color='{GRAY}'> / 100</font>",
            p("SC", alignment=TA_RIGHT, leading=32))

        header_tbl = Table(
            [[grade_para, score_para]],
            colWidths=[9 * cm, 8 * cm]
        )
        header_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX",    (0, 0), (-1, -1), 1.2, colors.HexColor(BRAND)),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SURFACE)),
            ("LEFTPADDING",   (0, 0), (-1, -1), 16),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 16),
            ("TOPPADDING",    (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ]))

        consistency = all_s.get("consistency", 0)
        trend = all_s.get("trend", "stable").upper()
        trend_sym = {"IMPROVING": "↑", "DECLINING": "↓", "STABLE": "→"}.get(trend, "→")
        trend_clr = {"IMPROVING": EMERALD, "DECLINING": ROSE, "STABLE": GRAY}.get(trend, GRAY)
        days = all_s.get("days", 0)

        meta_line = (
            f"<b>Specialist:</b> {emp_name}  &nbsp;&nbsp; "
            f"<b>Role:</b> {stype.title()}  &nbsp;&nbsp; "
            f"<b>Days Logged:</b> {days}  &nbsp;&nbsp; "
            f"<b>Consistency:</b> {consistency}%  &nbsp;&nbsp; "
            f"<b>Trend:</b> <font color='{trend_clr}'>{trend_sym} {trend}</font>"
        )
        if deduction > 0:
            meta_line += f"  &nbsp;&nbsp; <b><font color='{ROSE}'>Minus Marking: −{deduction} pts</font></b>"

        elems += [
            Paragraph("PHARMA IP SYSTEM", title_st),
            Paragraph("Official KRA Performance Analysis Report", sub_st),
            Paragraph(f"Generated: {date.today():%d %B %Y}", sub_st),
            Spacer(1, 0.25 * cm),
            HRFlowable(width="100%", thickness=2, color=colors.HexColor(BRAND)),
            Spacer(1, 0.35 * cm),
            header_tbl,
            Spacer(1, 0.2 * cm),
            Paragraph(meta_line, p("MT", fontSize=9, alignment=TA_CENTER,
                                   textColor=colors.HexColor(NAVY), leading=14)),
            Spacer(1, 0.15 * cm),
            Paragraph(
                f"<i>{all_s.get('feedback', '')}</i>",
                p("FB", fontSize=9, alignment=TA_CENTER,
                  textColor=colors.HexColor(GRAY), spaceAfter=8)
            ),
            Spacer(1, 0.15 * cm),
            HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(LGRAY)),
            Spacer(1, 0.3 * cm),
        ]

        # ── POINTS BREAKDOWN (role-aware, matches app.py) ────────────
        elems.append(Paragraph("POINTS BREAKDOWN", h2_st))
        elems.append(Paragraph(
            f"How the {score}/100 score was earned. Grades: ELITE ≥88 · PROFICIENT ≥72 · SATISFACTORY ≥52 · RE-TRAINING &lt;52.",
            p("PS", fontSize=8, textColor=colors.HexColor(GRAY), spaceAfter=6)
        ))

        if stype == "checker":
            pts_def = [
                ("Check Speed",          _gv(all_s, "check_speed"),            80.0, 30),
                ("Clearance Rate",       _gv(all_s, "clearance_rate", 100),    100.0, 25),
                ("Clean Check %",        _gv(all_s, "normal_pct"),             100.0, 20),
                ("Consistency",          _gv(all_s, "consistency"),            100.0, 10),
                ("Potential Efficiency", _gv(all_s, "potential_eff"),          100.0, 8),
                ("Volume",               _gv(all_s, "tck_total"),              500.0, 5),
                ("Sweep Bonus",          _gv(all_s, "tsd"),                    max(days, 1), 2),
            ]
        elif stype == "purchaser":
            pts_def = [
                ("Bill Processing Rate", _gv(all_s, "pur_bill_rate"),          100.0, 28),
                ("CS Fulfilment",        _gv(all_s, "pur_cs_fulfilment"),      100.0, 23),
                ("Racking Efficiency",   _gv(all_s, "pur_racking_eff"),        100.0, 18),
                ("Processing Speed",     _gv(all_s, "pur_speed"),              60.0, 12),
                ("Bill Entry Rate",      _gv(all_s, "pur_entry_rate"),         100.0, 8),
                ("Workspace",            _gv(all_s, "workspace_score"),        100.0, 4),
                ("Sweep Bonus",          _gv(all_s, "tsd"),                    max(days, 1), 2),
            ]
        else:  # picker
            pts_def = [
                ("Pick Accuracy",        _gv(all_s, "pick_acc"),               100.0, 25),
                ("Bill Fulfilment",      _gv(all_s, "bill_fulfilment"),        100.0, 20),
                ("Pick Speed",           _gv(all_s, "pick_speed"),             250.0, 18),
                ("Packing Efficiency",   _gv(all_s, "packing_eff"),            100.0, 12),
                ("Workspace",            _gv(all_s, "workspace_score"),        100.0, 10),
                ("CS Fulfilment",        _gv(all_s, "cs_fulfilment"),          100.0, 8),
                ("Consistency",          _gv(all_s, "consistency"),            100.0, 7),
            ]

        # Render each component as one row: [label, bar, earned/cap]
        pts_rows = []
        for label, val, denom, cap in pts_def:
            pct = (val / denom) if denom > 0 else 0
            if pct > 1:
                pct = 1
            earned = round(pct * cap, 1)
            pts_rows.append([
                Paragraph(f"<b>{label}</b>", pt_label_st),
                _points_bar_row(label, earned, cap, width_cm=9),
                Paragraph(f"{earned} <font color='{GRAY_2}'>/ {cap} pts</font>", pt_val_st),
            ])

        pt_tbl = Table(pts_rows, colWidths=[5 * cm, 9 * cm, 3.5 * cm])
        pt_tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.3, colors.HexColor(LGRAY)),
        ]))
        elems.append(pt_tbl)

        if deduction > 0:
            elems += [
                Spacer(1, 0.15 * cm),
                Table(
                    [[Paragraph(f"<font color='{ROSE}'><b>⚠ Minus Marking (complaint deductions)</b></font>",
                                p("DL", fontSize=9, leading=12)),
                      Paragraph(f"<font color='{ROSE}'><b>−{deduction} pts</b></font>",
                                p("DV", fontSize=11, alignment=TA_RIGHT, leading=12))]],
                    colWidths=[14 * cm, 3.5 * cm],
                    style=TableStyle([
                        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor(RO_LT)),
                        ("BOX",           (0, 0), (-1, -1), 0.6, colors.HexColor("#fca5a5")),
                        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
                        ("TOPPADDING",    (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ])
                ),
            ]

        elems += [Spacer(1, 0.35 * cm),
                  HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(LGRAY)),
                  Spacer(1, 0.2 * cm)]

        # ── CALCULATION METHODOLOGY (aligned with app.py) ────────────
        elems.append(Paragraph("CALCULATION METHODOLOGY", h2_st))
        if stype == "picker":
            formula_lines = [
                "Pick Accuracy (%)     = Picked ÷ (Picked + Missed) × 100",
                "Bill Fulfilment (%)   = Sales Bill Picked ÷ Total Bills Received × 100",
                "Pick Speed (/hr)      = (Picked + Missed) ÷ (9 hrs × Days)",
                "Packing Efficiency    = Packing Done ÷ Sales Bill Picked × 100",
                "CS Fulfilment (%)     = min(Packing Done ÷ CS Sales Open × 100, 100)",
                "Workspace Score       = Rack×50% + Table×30% + Sweep×20%",
                "Points (max 100)      = Acc×25 + Fulfilment×20 + Speed÷250×18 + Packing×12 + Workspace×10 + CS×8 + Consistency×7",
                "Minus Marking         = Sum of pending complaint deductions (capped at 30 pts)",
                "Potential Items       = 200 items/hr × 9 hrs × Days",
                "Consistency (%)       = max(0, 100 − std_dev(daily_accuracy) × 2)",
                "Grade                 = ELITE ≥ 88 | PROFICIENT ≥ 72 | SATISFACTORY ≥ 52 | RE-TRAINING < 52",
            ]
        elif stype == "checker":
            formula_lines = [
                "Total SB Checked      = SB Normal + SB Urgent (urgent done first)",
                "Total Items Checked   = Items Normal + Urgent Items",
                "Clean Check %         = Normal Items ÷ Total Items × 100",
                "Pending Bills         = Manually entered by checker",
                "Clearance Rate (%)    = SB Checked ÷ (SB Checked + Pending) × 100",
                "Check Speed (/hr)     = Total Items Checked ÷ (9 hrs × Days)",
                "Points (max 100)      = Speed÷80×30 + Clearance×25 + CleanCheck×20 + Consistency×10 + PotEff×8 + Volume×5 + Sweep×2",
                "Minus Marking         = Sum of pending complaint deductions (capped at 30 pts)",
                "Potential Items       = 25 items/hr × 9 hrs × Days",
                "Consistency (%)       = max(0, 100 − std_dev(daily_speed) × 2)",
                "Grade                 = ELITE ≥ 88 | PROFICIENT ≥ 72 | SATISFACTORY ≥ 52 | RE-TRAINING < 52",
            ]
        else:  # purchaser
            formula_lines = [
                "Bill Processing Rate  = PO Bills Checked ÷ PO Bills Received × 100",
                "CS Fulfilment (%)     = CS in PO Received ÷ CS in PO Open × 100",
                "Racking Efficiency    = Items Racked ÷ Number of Items × 100",
                "Processing Speed      = Number of Items ÷ (9 hrs × Days)",
                "Bill Entry Rate       = PO Bill Entry ÷ PO Bills Received × 100",
                "Pending PO Bills      = max(PO Bills Received − PO Bills Checked, 0)",
                "Points (max 100)      = BillRate×28 + CS×23 + Racking×18 + Speed÷60×12 + Entry×8 + Pending×5 + Workspace×4 + Sweep×2",
                "Minus Marking         = Sum of pending complaint deductions (capped at 30 pts)",
                "Grade                 = ELITE ≥ 88 | PROFICIENT ≥ 72 | SATISFACTORY ≥ 52 | RE-TRAINING < 52",
            ]
        for line in formula_lines:
            elems.append(Paragraph(line, formula_st))
        elems.append(Spacer(1, 0.25 * cm))

        # ── KPI TABLE (role-aware) ───────────────────────────────────
        elems.append(Paragraph("KPI PERFORMANCE TABLE", h2_st))
        if stype == "picker":
            rows_data = [
                ["Metric", "Today", "This Week", "This Month", "All-Time"],
                ["Pick Accuracy",       _sv(d_s, "pick_acc", "%"),         _sv(w_s, "pick_acc", "%"),         _sv(m_s, "pick_acc", "%"),         _sv(all_s, "pick_acc", "%")],
                ["Bill Fulfilment %",   _sv(d_s, "bill_fulfilment", "%"),  _sv(w_s, "bill_fulfilment", "%"),  _sv(m_s, "bill_fulfilment", "%"),  _sv(all_s, "bill_fulfilment", "%")],
                ["Total Bills Rcvd",    _sv(d_s, "tbr_picker"),            _sv(w_s, "tbr_picker"),            _sv(m_s, "tbr_picker"),            _sv(all_s, "tbr_picker")],
                ["Items Picked",        _sv(d_s, "tp"),                    _sv(w_s, "tp"),                    _sv(m_s, "tp"),                    _sv(all_s, "tp")],
                ["Items Missed",        _sv(d_s, "tm"),                    _sv(w_s, "tm"),                    _sv(m_s, "tm"),                    _sv(all_s, "tm")],
                ["Pick Speed /hr",      _sv(d_s, "pick_speed"),            _sv(w_s, "pick_speed"),            _sv(m_s, "pick_speed"),            _sv(all_s, "pick_speed")],
                ["Sales Bill Picked",   _sv(d_s, "tsb"),                   _sv(w_s, "tsb"),                   _sv(m_s, "tsb"),                   _sv(all_s, "tsb")],
                ["Packing Done",        _sv(d_s, "tpd"),                   _sv(w_s, "tpd"),                   _sv(m_s, "tpd"),                   _sv(all_s, "tpd")],
                ["Packing Eff. %",      _sv(d_s, "packing_eff", "%"),      _sv(w_s, "packing_eff", "%"),      _sv(m_s, "packing_eff", "%"),      _sv(all_s, "packing_eff", "%")],
                ["CS Sales Open",       _sv(d_s, "tcs"),                   _sv(w_s, "tcs"),                   _sv(m_s, "tcs"),                   _sv(all_s, "tcs")],
                ["CS Fulfilment %",     _sv(d_s, "cs_fulfilment", "%"),    _sv(w_s, "cs_fulfilment", "%"),    _sv(m_s, "cs_fulfilment", "%"),    _sv(all_s, "cs_fulfilment", "%")],
                ["Workspace Score %",   _sv(d_s, "workspace_score", "%"),  _sv(w_s, "workspace_score", "%"),  _sv(m_s, "workspace_score", "%"),  _sv(all_s, "workspace_score", "%")],
                ["Consistency %",       _sv(d_s, "consistency", "%"),      _sv(w_s, "consistency", "%"),      _sv(m_s, "consistency", "%"),      _sv(all_s, "consistency", "%")],
                ["Potential Items",     _sv(d_s, "potential_items"),       _sv(w_s, "potential_items"),       _sv(m_s, "potential_items"),       _sv(all_s, "potential_items")],
                ["Gap Items",           _sv(d_s, "gap_items"),             _sv(w_s, "gap_items"),             _sv(m_s, "gap_items"),             _sv(all_s, "gap_items")],
                ["Points Score",        _sv(d_s, "eff_score"),             _sv(w_s, "eff_score"),             _sv(m_s, "eff_score"),             _sv(all_s, "eff_score")],
                ["Grade",               _sv(d_s, "grade"),                 _sv(w_s, "grade"),                 _sv(m_s, "grade"),                 _sv(all_s, "grade")],
            ]
        elif stype == "checker":
            rows_data = [
                ["Metric", "Today", "This Week", "This Month", "All-Time"],
                ["SB Normal",           _sv(d_s, "tsb_normal"),            _sv(w_s, "tsb_normal"),            _sv(m_s, "tsb_normal"),            _sv(all_s, "tsb_normal")],
                ["SB Urgent",           _sv(d_s, "tsb_urgent"),            _sv(w_s, "tsb_urgent"),            _sv(m_s, "tsb_urgent"),            _sv(all_s, "tsb_urgent")],
                ["Total SB Checked",    _sv(d_s, "tsb_total"),             _sv(w_s, "tsb_total"),             _sv(m_s, "tsb_total"),             _sv(all_s, "tsb_total")],
                ["Items Normal",        _sv(d_s, "tck_normal"),            _sv(w_s, "tck_normal"),            _sv(m_s, "tck_normal"),            _sv(all_s, "tck_normal")],
                ["Items Urgent",        _sv(d_s, "tck_urgent"),            _sv(w_s, "tck_urgent"),            _sv(m_s, "tck_urgent"),            _sv(all_s, "tck_urgent")],
                ["Total Items Checked", _sv(d_s, "tck_total"),             _sv(w_s, "tck_total"),             _sv(m_s, "tck_total"),             _sv(all_s, "tck_total")],
                ["Clean Check %",       _sv(d_s, "normal_pct", "%"),       _sv(w_s, "normal_pct", "%"),       _sv(m_s, "normal_pct", "%"),       _sv(all_s, "normal_pct", "%")],
                ["Urgent %",            _sv(d_s, "urgent_pct", "%"),       _sv(w_s, "urgent_pct", "%"),       _sv(m_s, "urgent_pct", "%"),       _sv(all_s, "urgent_pct", "%")],
                ["Bills Received",      _sv(d_s, "tbr"),                   _sv(w_s, "tbr"),                   _sv(m_s, "tbr"),                   _sv(all_s, "tbr")],
                ["Pending Bills",       _sv(d_s, "pending_bills"),         _sv(w_s, "pending_bills"),         _sv(m_s, "pending_bills"),         _sv(all_s, "pending_bills")],
                ["Clearance Rate %",    _sv(d_s, "clearance_rate", "%"),   _sv(w_s, "clearance_rate", "%"),   _sv(m_s, "clearance_rate", "%"),   _sv(all_s, "clearance_rate", "%")],
                ["Check Speed /hr",     _sv(d_s, "check_speed"),           _sv(w_s, "check_speed"),           _sv(m_s, "check_speed"),           _sv(all_s, "check_speed")],
                ["Packing Done",        _sv(d_s, "tpk"),                   _sv(w_s, "tpk"),                   _sv(m_s, "tpk"),                   _sv(all_s, "tpk")],
                ["Consistency %",       _sv(d_s, "consistency", "%"),      _sv(w_s, "consistency", "%"),      _sv(m_s, "consistency", "%"),      _sv(all_s, "consistency", "%")],
                ["Potential Items",     _sv(d_s, "potential_items"),       _sv(w_s, "potential_items"),       _sv(m_s, "potential_items"),       _sv(all_s, "potential_items")],
                ["Gap Items",           _sv(d_s, "gap_items"),             _sv(w_s, "gap_items"),             _sv(m_s, "gap_items"),             _sv(all_s, "gap_items")],
                ["Points Score",        _sv(d_s, "eff_score"),             _sv(w_s, "eff_score"),             _sv(m_s, "eff_score"),             _sv(all_s, "eff_score")],
                ["Grade",               _sv(d_s, "grade"),                 _sv(w_s, "grade"),                 _sv(m_s, "grade"),                 _sv(all_s, "grade")],
            ]
        else:  # purchaser
            rows_data = [
                ["Metric", "Today", "This Week", "This Month", "All-Time"],
                ["PO Bills Received",   _sv(d_s, "pur_bills_received"),     _sv(w_s, "pur_bills_received"),     _sv(m_s, "pur_bills_received"),     _sv(all_s, "pur_bills_received")],
                ["PO Bills Checked",    _sv(d_s, "pur_bills_checked"),      _sv(w_s, "pur_bills_checked"),      _sv(m_s, "pur_bills_checked"),      _sv(all_s, "pur_bills_checked")],
                ["PO Bill Entry",       _sv(d_s, "pur_bill_entry"),         _sv(w_s, "pur_bill_entry"),         _sv(m_s, "pur_bill_entry"),         _sv(all_s, "pur_bill_entry")],
                ["Pending PO Bills",    _sv(d_s, "pur_pending_bills"),      _sv(w_s, "pur_pending_bills"),      _sv(m_s, "pur_pending_bills"),      _sv(all_s, "pur_pending_bills")],
                ["Number of Items",     _sv(d_s, "pur_items"),              _sv(w_s, "pur_items"),              _sv(m_s, "pur_items"),              _sv(all_s, "pur_items")],
                ["CS in PO Open",       _sv(d_s, "pur_cs_open"),            _sv(w_s, "pur_cs_open"),            _sv(m_s, "pur_cs_open"),            _sv(all_s, "pur_cs_open")],
                ["CS in PO Received",   _sv(d_s, "pur_cs_received"),        _sv(w_s, "pur_cs_received"),        _sv(m_s, "pur_cs_received"),        _sv(all_s, "pur_cs_received")],
                ["Items Racked",        _sv(d_s, "pur_items_racked"),       _sv(w_s, "pur_items_racked"),       _sv(m_s, "pur_items_racked"),       _sv(all_s, "pur_items_racked")],
                ["Bill Processing %",   _sv(d_s, "pur_bill_rate", "%"),     _sv(w_s, "pur_bill_rate", "%"),     _sv(m_s, "pur_bill_rate", "%"),     _sv(all_s, "pur_bill_rate", "%")],
                ["CS Fulfilment %",     _sv(d_s, "pur_cs_fulfilment", "%"), _sv(w_s, "pur_cs_fulfilment", "%"), _sv(m_s, "pur_cs_fulfilment", "%"), _sv(all_s, "pur_cs_fulfilment", "%")],
                ["Racking Eff. %",      _sv(d_s, "pur_racking_eff", "%"),   _sv(w_s, "pur_racking_eff", "%"),   _sv(m_s, "pur_racking_eff", "%"),   _sv(all_s, "pur_racking_eff", "%")],
                ["Processing Speed",    _sv(d_s, "pur_speed"),              _sv(w_s, "pur_speed"),              _sv(m_s, "pur_speed"),              _sv(all_s, "pur_speed")],
                ["Consistency %",       _sv(d_s, "consistency", "%"),       _sv(w_s, "consistency", "%"),       _sv(m_s, "consistency", "%"),       _sv(all_s, "consistency", "%")],
                ["Points Score",        _sv(d_s, "eff_score"),              _sv(w_s, "eff_score"),              _sv(m_s, "eff_score"),              _sv(all_s, "eff_score")],
                ["Grade",               _sv(d_s, "grade"),                  _sv(w_s, "grade"),                  _sv(m_s, "grade"),                  _sv(all_s, "grade")],
            ]

        ct = Table(rows_data, colWidths=[4.2 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm])
        ct.setStyle(TableStyle([
            ("BACKGROUND",     (0, 0), (-1, 0),  colors.HexColor(NAVY)),
            ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE",       (0, 0), (-1, -1), 8),
            ("ALIGN",          (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",          (0, 1), (0, -1),  "LEFT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor(SURFACE), colors.white]),
            ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor(LGRAY)),
            ("TOPPADDING",     (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
            # Highlight the last two rows (Points Score, Grade)
            ("BACKGROUND",     (0, -2), (-1, -1), colors.HexColor(BRAND_LT)),
            ("FONTNAME",       (0, -2), (-1, -1), "Helvetica-Bold"),
            ("TEXTCOLOR",      (0, -2), (-1, -1), colors.HexColor(NAVY)),
        ]))
        elems += [ct, Spacer(1, 0.4 * cm)]

        # ── PIE CHARTS ───────────────────────────────────────────────
        elems.append(Paragraph("PERFORMANCE BREAKDOWN", h2_st))
        cur_eff = all_s.get("eff_score", 0)

        if stype == "checker":
            tck_n = all_s.get("tck_normal", 0) or 0
            tck_u = all_s.get("tck_urgent", 0) or 0
            p1 = _make_pie(["Normal", "Urgent"], [tck_n, tck_u],
                           [BRAND, PURPLE], "Normal vs Urgent Items")
            tbr = all_s.get("tbr", 0) or 0
            pending = all_s.get("pending_bills", 0) or 0
            cleared = max(tbr - pending, 0)
            p2 = _make_pie(["Cleared", "Pending"], [cleared, pending],
                           [EMERALD, ROSE], "Bill Clearance")
        elif stype == "purchaser":
            pur_bc = all_s.get("pur_bills_checked", 0) or 0
            pur_br = all_s.get("pur_bills_received", 0) or 0
            p1 = _make_pie(["Checked", "Pending"], [pur_bc, max(pur_br - pur_bc, 0)],
                           [BRAND, "#bfdbfe"], "Bill Processing")
            cs_r = all_s.get("pur_cs_received", 0) or 0
            cs_o = all_s.get("pur_cs_open", 0) or 0
            p2 = _make_pie(["Received", "Pending"], [cs_r, max(cs_o - cs_r, 0)],
                           [EMERALD, AM_LT], "CS Fulfilment")
        else:  # picker
            tp = all_s.get("tp", 0) or 0
            tm = all_s.get("tm", 0) or 0
            p1 = _make_pie(["Picked", "Missed"], [tp, tm],
                           [BRAND, ROSE], "Items Picked vs Missed")
            tpd = all_s.get("tpd", 0) or 0
            tsb = all_s.get("tsb", 0) or 0
            p2 = _make_pie(["Packed", "Remaining"], [tpd, max(tsb - tpd, 0)],
                           [EMERALD, "#bfdbfe"], "Sales Bills vs Packed")

        p3 = _make_pie(
            ["Earned", "Gap"],
            [cur_eff, max(100 - cur_eff, 0)],
            [GRADE_COLORS.get(grade, NAVY), LGRAY],
            "Points Earned vs Potential"
        )

        pt = Table([[p1, p2, p3]])
        pt.setStyle(TableStyle([
            ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elems += [pt, Spacer(1, 0.3 * cm)]

        # ── BAR CHARTS (role-aware) ──────────────────────────────────
        elems.append(Paragraph("PERIOD TREND", h2_st))
        lbls = ["Today", "Week", "Month", "All"]

        def _series(k):
            return [_gv(d_s, k), _gv(w_s, k), _gv(m_s, k), _gv(all_s, k)]

        if stype == "checker":
            bt_rows = [
                [_make_bars(_series("check_speed"),    lbls, "Check Speed /hr",  max_val=80)],
                [_make_bars(_series("clearance_rate"), lbls, "Clearance Rate %", max_val=100)],
                [_make_bars(_series("normal_pct"),     lbls, "Clean Check %",    max_val=100)],
                [_make_bars(_series("eff_score"),      lbls, "Points Score",     max_val=100)],
            ]
        elif stype == "purchaser":
            bt_rows = [
                [_make_bars(_series("pur_bill_rate"),     lbls, "Bill Processing %", max_val=100)],
                [_make_bars(_series("pur_cs_fulfilment"), lbls, "CS Fulfilment %",   max_val=100)],
                [_make_bars(_series("pur_racking_eff"),   lbls, "Racking Eff. %",    max_val=100)],
                [_make_bars(_series("eff_score"),         lbls, "Points Score",      max_val=100)],
            ]
        else:  # picker
            bt_rows = [
                [_make_bars(_series("pick_acc"),        lbls, "Pick Accuracy %",   max_val=100)],
                [_make_bars(_series("bill_fulfilment"), lbls, "Bill Fulfilment %", max_val=100)],
                [_make_bars(_series("packing_eff"),     lbls, "Packing Eff. %",    max_val=100)],
                [_make_bars(_series("eff_score"),       lbls, "Points Score",      max_val=100)],
            ]

        bt = Table(bt_rows)
        bt.setStyle(TableStyle([
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elems += [bt, Spacer(1, 0.3 * cm)]

        # ── DAILY LOG (role-aware) ───────────────────────────────────
        if entries:
            elems += [
                PageBreak(),
                Paragraph("DAILY ENTRY LOG", h2_st),
                Paragraph(
                    f"Last {len(entries)} entries, newest first.",
                    p("NT", fontSize=8, textColor=colors.HexColor(GRAY), spaceAfter=6)
                ),
            ]
            if stype == "picker":
                log = [["Date", "Bills Rcvd", "SB Picked", "Picked", "Missed", "Acc %",
                        "Packing", "CS Open", "Rack", "Table", "Sweep", "Hrs"]]
                for e in entries:
                    log.append([
                        str(e.entry_date),
                        int(getattr(e, "total_bills_received", 0) or 0),
                        int(e.sales_bills_open or 0),
                        int(e.picked or 0),
                        int(e.missed or 0),
                        f"{e.accuracy}%",
                        int(e.packing_done or 0),
                        int(e.cs_sales_open or 0),
                        int(e.rack_organized or 0),
                        "✓" if e.table_clean else "—",
                        "✓" if getattr(e, "sweep_done", 0) else "—",
                        round(float(e.total_time or 9.0), 2),
                    ])
                cws = [2.0*cm, 1.6*cm, 1.5*cm, 1.3*cm, 1.3*cm, 1.3*cm,
                       1.5*cm, 1.4*cm, 1.2*cm, 1.2*cm, 1.2*cm, 1.1*cm]

            elif stype == "checker":
                log = [["Date", "SB Normal", "SB Urgent", "Items", "Urgent",
                        "Total SB", "Total Items", "Bills Rcvd", "Pending", "Clear %", "Speed/hr"]]
                for e in entries:
                    sb_total = (e.sales_bills_open or 0) + (e.cs_sales_open or 0)
                    items_total = (e.checked or 0) + (e.errors_found or 0)
                    br = int(getattr(e, "bills_received", 0) or 0)
                    pending = max(br - sb_total, 0)
                    clearance = round(sb_total / br * 100, 1) if br > 0 else 100.0
                    speed = round(items_total / 9.0, 1)
                    log.append([
                        str(e.entry_date),
                        int(e.sales_bills_open or 0),
                        int(e.cs_sales_open or 0),
                        int(e.checked or 0),
                        int(e.errors_found or 0),
                        sb_total,
                        items_total,
                        br,
                        pending,
                        f"{clearance}%",
                        f"{speed}/hr",
                    ])
                cws = [2.0*cm, 1.5*cm, 1.5*cm, 1.3*cm, 1.3*cm, 1.5*cm,
                       1.7*cm, 1.5*cm, 1.3*cm, 1.3*cm, 1.5*cm]

            else:  # purchaser
                log = [["Date", "PO Rcvd", "PO Chkd", "Bill Entry", "Items",
                        "CS Open", "CS Rcvd", "Racked", "Bill %", "Rack %", "Hrs"]]
                for e in entries:
                    po_r = int(e.sales_bills_open or 0)
                    po_c = int(e.checked or 0)
                    items = int(e.errors_found or 0)
                    racked = int(e.rack_organized or 0)
                    bill_pct = round(po_c / po_r * 100, 1) if po_r > 0 else 0
                    rack_pct = round(racked / items * 100, 1) if items > 0 else 0
                    log.append([
                        str(e.entry_date),
                        po_r,
                        po_c,
                        int(e.picked or 0),
                        items,
                        int(e.cs_sales_open or 0),
                        int(e.packing_done or 0),
                        racked,
                        f"{bill_pct}%",
                        f"{rack_pct}%",
                        round(float(e.total_time or 9.0), 2),
                    ])
                cws = [2.0*cm, 1.4*cm, 1.4*cm, 1.5*cm, 1.3*cm, 1.3*cm,
                       1.3*cm, 1.3*cm, 1.3*cm, 1.3*cm, 1.1*cm]

            lt = Table(log, colWidths=cws, repeatRows=1)
            lt.setStyle(TableStyle([
                ("BACKGROUND",     (0, 0), (-1, 0),  colors.HexColor(NAVY)),
                ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.white),
                ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
                ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE",       (0, 0), (-1, -1), 7.2),
                ("ALIGN",          (0, 0), (-1, -1), "CENTER"),
                ("ALIGN",          (0, 1), (0, -1),  "LEFT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor(SURFACE), colors.white]),
                ("GRID",           (0, 0), (-1, -1), 0.3, colors.HexColor(LGRAY)),
                ("TOPPADDING",     (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
            ]))
            elems += [lt, Spacer(1, 0.3 * cm)]

        # ── FOOTER ───────────────────────────────────────────────────
        elems += [
            Spacer(1, 0.2 * cm),
            HRFlowable(width="100%", thickness=1.5, color=colors.HexColor(BRAND)),
            Spacer(1, 0.15 * cm),
            Paragraph(
                f"Confidential — Pharma IP System · Generated {date.today():%d %b %Y} · Internal use only.",
                p("FT", fontSize=7.5, alignment=TA_CENTER,
                  textColor=colors.HexColor(GRAY))
            ),
        ]

        _doc.build(elems)
        _buf.seek(0)
        return _buf

    except Exception as _e:
        logger.error(f"PDF generation error: {_e}")
        import traceback
        traceback.print_exc()
        _buf2 = io.BytesIO()
        _S2   = getSampleStyleSheet()
        _doc2 = SimpleDocTemplate(_buf2, pagesize=A4)
        _doc2.build([
            Paragraph("Pharma IP System", _S2["Title"]),
            Paragraph("PDF generation encountered an error.", _S2["Normal"]),
            Paragraph(f"Details: {_e}", _S2["Normal"]),
            Paragraph("Please contact your system administrator.", _S2["Normal"]),
        ])
        _buf2.seek(0)
        return _buf2
