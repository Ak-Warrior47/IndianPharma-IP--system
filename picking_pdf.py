"""Picking report and staff day report as PDFs (landscape A4) from pick_report().

Sections: summary figures, leaderboard, items and bills per day (chart),
every bill, missing items, late / waiting log, and each picker's bills.
Long tables continue across pages with their header repeated.
"""
import io
from datetime import datetime, timedelta

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

INK = colors.HexColor("#16202F")
INK3 = colors.HexColor("#64748B")
LINE = colors.HexColor("#DFE4EC")
GREEN = colors.HexColor("#146B4A")
BLUE = colors.HexColor("#1E40AF")
RED = colors.HexColor("#B91C1C")
AMBER = colors.HexColor("#B45309")
TINT = colors.HexColor("#F3F6FA")

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Heading1"], fontName="Helvetica-Bold", fontSize=18, textColor=INK, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, textColor=INK, spaceBefore=10, spaceAfter=5)
SUB = ParagraphStyle("sub", parent=_ss["Normal"], fontName="Helvetica", fontSize=9.5, textColor=INK3)
CELL = ParagraphStyle("cell", parent=_ss["Normal"], fontName="Helvetica", fontSize=8.3, leading=10, textColor=INK, alignment=TA_LEFT)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")


def _p(text, bold=False):
    return Paragraph(str(text if text is not None else ""), CELLB if bold else CELL)


def _table(head, rows, widths, red_rows=(), align_right=()):
    data = [[_p(h, True) for h in head]] + [[c if isinstance(c, Paragraph) else _p(c) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    st = [("BACKGROUND", (0, 0), (-1, 0), TINT), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK3),
          ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    for i in red_rows:
        st.append(("TEXTCOLOR", (0, i + 1), (-1, i + 1), RED))
    t.setStyle(TableStyle(st))
    return t


def _cards(items, width):
    """A row of summary figures."""
    n = len(items)
    w = width / n
    lab = ParagraphStyle("lab", parent=CELL, fontSize=7, leading=9, textColor=INK3)
    val = ParagraphStyle("val", parent=CELL, fontName="Helvetica-Bold", fontSize=15, leading=18)
    cells = [[Paragraph(str(k).upper(), lab) for k, _ in items], [Paragraph(str(v), val) for _, v in items]]
    t = Table(cells, colWidths=[w] * n, hAlign="LEFT")
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("LINEAFTER", (0, 0), (-2, -1), 0.4, LINE),
                           ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
                           ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _chart(series, width):
    d = Drawing(width, 150)
    if not series:
        return d
    c = VerticalBarChart()
    c.x, c.y, c.width, c.height = 30, 22, width - 50, 110
    c.data = [[s["bills"] for s in series], [s["items"] for s in series]]
    c.categoryAxis.categoryNames = [s["day"] for s in series]
    c.categoryAxis.labels.fontSize = 6.5 if len(series) > 14 else 7.5
    c.categoryAxis.labels.angle = 45 if len(series) > 10 else 0
    c.categoryAxis.labels.dy = -6 if len(series) > 10 else -2
    c.valueAxis.valueMin = 0
    c.valueAxis.labels.fontSize = 7.5
    top = max([max(s["items"], s["bills"]) for s in series] + [1])
    c.valueAxis.valueMax = top + max(1, int(top * 0.1))
    c.bars[0].fillColor, c.bars[1].fillColor = BLUE, GREEN
    c.barSpacing, c.groupSpacing = 1, 6
    d.add(c)
    d.add(String(32, 140, "Bills", fontSize=8, fillColor=BLUE, fontName="Helvetica-Bold"))
    d.add(String(62, 140, "Items", fontSize=8, fillColor=GREEN, fontName="Helvetica-Bold"))
    return d


def _rules(r):
    n, u = r.get("times") or (35, 30)
    half = r.get("extra_seconds", 125) / 2.0
    return ("Time per item: %d s normal bill, %d s urgent bill; then %g s extra time and %g s report time, then late."
            % (n, u, half, half))


def _made():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p").replace(" 0", " ")


def build_picking_pdf(r):
    buf = io.BytesIO()
    page = landscape(A4)
    period = r["day_from"].strftime("%d %b %Y") + ("" if r["single_day"] else " – " + r["day_to"].strftime("%d %b %Y"))
    made = _made()

    def frame(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(GREEN)
        canvas.rect(0, page[1] - 7 * mm, page[0], 7 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(12 * mm, page[1] - 4.8 * mm, "INDIAN PHARMA · Picking report")
        canvas.drawRightString(page[0] - 12 * mm, page[1] - 4.8 * mm, period)
        canvas.setFillColor(INK3)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(12 * mm, 7 * mm, "Made %s (Indian time). %s" % (made, _rules(r)))
        canvas.drawRightString(page[0] - 12 * mm, 7 * mm, "Page %d" % doc.page)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=13 * mm,
                            title="Picking report %s" % period, author="Indian Pharma")
    W = page[0] - 24 * mm
    T = r["totals"]
    story = [Paragraph("Picking report", H1),
             Paragraph("%s · %s%s" % (period, r["picker_name"] or "All pickers",
                                      (" · Day closed: %d bills (checkers' day total)" % r["closed"]) if r.get("closed") is not None else ""), SUB),
             Spacer(1, 6),
             _cards([("Bills", T["bills"]), ("Items", T["items"]), ("Picked", T["picked"]), ("Missing reported", T["missing"]),
                     ("Urgent bills", T["urgent"]), ("Avg minutes / bill", T["avg_minutes"] if T["avg_minutes"] is not None else "—"),
                     ("Late grabs", r["late"]), ("Left waiting", r["idle"])], W)]

    story.append(Paragraph("Leaderboard", H2))
    if r["board"]:
        story.append(_table(["#", "Picker", "Bills", "Items", "On time", "Points", "Items/hr", "Missing", "Late", "Left waiting"],
                            [[x["rank"], x["name"], x["bills"], x["items"], ("%d%%" % round(x["on_time_pct"])) if x["grabs"] else "—",
                              x["points"], x["rate"] or "—", x["missing"], x["late"], x["idle"]] for x in r["board"]],
                            [10 * mm, 52 * mm] + [(W - 62 * mm) / 8] * 8))
    else:
        story.append(Paragraph("No pickers.", SUB))

    if len(r["series"]) > 1:
        story.append(KeepTogether([Paragraph("Bills and items per day", H2), _chart(r["series"], W)]))

    story.append(Paragraph("Bills (%d)" % len(r["bills"]), H2))
    if r["bills"]:
        status = {"open": "Open", "done": "Complete", "cancelled": "Cancelled", "scheduled": "Rescheduled"}
        rows, red = [], []
        for i, b in enumerate(r["bills"]):
            rows.append([b["date"], b["bill_no"] + (" (U)" if b["urgent"] else ""), b["items"], b["picked"], b["missing"] or "",
                         b["pickers"] or "—", b["late"] or "", b["added"], b["completed"] or "—",
                         b["minutes"] if b["minutes"] is not None else "—", status.get(b["status"], b["status"])])
            if b["late"] or b["missing"]:
                red.append(i)
        story.append(_table(["Date", "Bill number", "Items", "Picked", "Missing", "Pickers (items)", "Late", "Added", "Completed", "Minutes", "Status"],
                            rows, [16 * mm, 30 * mm, 13 * mm, 13 * mm, 14 * mm, W - 207 * mm, 11 * mm, 20 * mm, 22 * mm, 16 * mm, 22 * mm], red_rows=red))
    else:
        story.append(Paragraph("No bills in this period.", SUB))

    story.append(Paragraph("Missing items (%d)" % len(r["missing"]), H2))
    if r["missing"]:
        story.append(_table(["Date", "Bill number", "Item", "Qty", "Reported by", "At", "What happened"],
                            [[m["date"], m["bill_no"], m["item"], m["qty"], m["by"], m["at"], m["status"]] for m in r["missing"]],
                            [16 * mm, 32 * mm, W - 164 * mm, 12 * mm, 40 * mm, 22 * mm, 42 * mm]))
    else:
        story.append(Paragraph("No missing items reported.", SUB))

    story.append(Paragraph("Late report (%d)" % len(r["events"]), H2))
    if r["events"]:
        def what(e):
            if e["kind"] == "late":
                return "Late on %s%s" % (e["bill"], (" — %g min" % e["minutes"]) if e["minutes"] else "")
            if e["kind"] == "idle":
                return "Left %s waiting %g min" % (e["bill"], e["minutes"] or 0)
            return "Could not find %s on %s" % (e["detail"], e["bill"])
        story.append(_table(["Date", "Time", "Type", "Picker", "What"],
                            [[e["date"], e["at"], e["label"], e["who"], what(e)] for e in r["events"]],
                            [16 * mm, 20 * mm, 20 * mm, 40 * mm, W - 96 * mm],
                            red_rows=[i for i, e in enumerate(r["events"]) if e["kind"] == "late"]))
    else:
        story.append(Paragraph("Nobody was late or left a bill waiting.", SUB))

    with_bills = [x for x in r["board"] if x["bill_list"]]
    if with_bills:
        story.append(PageBreak())
        story.append(Paragraph("Each picker, bill by bill", H2))
        for x in with_bills:
            block = [Paragraph("<b>%s</b> — %d bills · %d items · %s on time · %s points" % (
                x["name"], x["bills"], x["items"], ("%d%%" % round(x["on_time_pct"])) if x["grabs"] else "—", x["points"]), CELL), Spacer(1, 3),
                _table(["Date", "Bill number", "Items picked", "Not found", "Grabs", "Points", "Late"],
                       [[bl["day"], bl["bill_no"] + (" (U)" if bl["urgent"] else ""), bl["items"], bl["missing"] or "", bl["grabs"],
                         bl["points"], "yes" if bl["late"] else ""] for bl in x["bill_list"]],
                       [20 * mm, 40 * mm, 26 * mm, 22 * mm, 18 * mm, 20 * mm, 18 * mm],
                       red_rows=[i for i, bl in enumerate(x["bill_list"]) if bl["late"]]), Spacer(1, 8)]
            story.append(KeepTogether(block) if len(x["bill_list"]) < 18 else block[0])
            if len(x["bill_list"]) >= 18:
                story.extend(block[1:])
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()


def build_staff_day_pdf(r):
    """Staff day report: everyone who came in, sign-in times, and every
    picker's grabs with all their times (from staff_day_report)."""
    buf = io.BytesIO()
    page = landscape(A4)
    day = r["day"].strftime("%a %d %b %Y")
    made = _made()

    def frame(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(GREEN)
        canvas.rect(0, page[1] - 7 * mm, page[0], 7 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(12 * mm, page[1] - 4.8 * mm, "INDIAN PHARMA · Staff day report")
        canvas.drawRightString(page[0] - 12 * mm, page[1] - 4.8 * mm, day)
        canvas.setFillColor(INK3)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(12 * mm, 7 * mm, "Made %s (Indian time). %s" % (made, _rules(r)))
        canvas.drawRightString(page[0] - 12 * mm, 7 * mm, "Page %d" % doc.page)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=13 * mm,
                            title="Staff day report %s" % day, author="Indian Pharma")
    W = page[0] - 24 * mm
    T = r["totals"]
    story = [Paragraph("Staff day report", H1),
             Paragraph("%s · %s" % (day, r["emp_name"] or "Everyone who came in"), SUB), Spacer(1, 6),
             _cards([("People in", T["people"]), ("Picked today", T["pickers"]), ("Items picked", T["items"]),
                     ("Pick points", T["points"]), ("Late grabs", T["late"]), ("Not found", T["missing"])], W)]

    story.append(Paragraph("Who came in (%d)" % len(r["people"]), H2))
    if r["people"]:
        def work(p):
            bits = []
            if p["pick"]["grabs"]:
                bits.append("%d bill%s, %d items, %s pts" % (p["pick"]["bills"], "" if p["pick"]["bills"] == 1 else "s",
                                                          p["pick"]["items"], p["pick"]["points"]))
            return "; ".join(bits) or "—"
        story.append(_table(["Name", "Role", "Signed in", "Signed out", "Last seen", "Extra role", "Work"],
                            [[p["name"], p["role"], (p["first_in"] or "—") + ((" (%d times, last %s)" % (p["logins"], p["last_in"])) if p["logins"] > 1 else ""),
                              p["last_out"] or "—", p["last_seen"] or "—", ", ".join(p["multitask"]) or "—", work(p)] for p in r["people"]],
                            [36 * mm, 30 * mm, 40 * mm, 20 * mm, 20 * mm, 26 * mm, W - 172 * mm]))
    else:
        story.append(Paragraph("Nobody signed in or worked on this day.", SUB))

    for p in [p for p in r["people"] if p["picks"]]:
        k = p["pick"]
        story.append(PageBreak())
        story.append(Paragraph("%s — %s" % (p["name"], p["role"]), H2))
        story.append(Paragraph("Signed in %s%s · first pick %s · last Done %s" % (
            p["first_in"] or "—", (" · signed out %s" % p["last_out"]) if p["last_out"] else "", k["first_take"] or "—", k["last_done"] or "—"), SUB))
        story.append(Spacer(1, 4))
        story.append(_cards([("Bills", k["bills"]), ("Items", k["items"]), ("Points", k["points"]),
                             ("Normal / extra / report / late", "%d / %d / %d / %d" % (k["ontime"], k["extra"], k["report"], k["late"])),
                             ("Picking time", k["picking_time"] or "—"), ("Free time", k["free_time"] or "—"),
                             ("Items / hour", k["rate"] or "—"), ("Not found", k["missing"])], W))
        story.append(Spacer(1, 6))
        rows, red = [], []
        for i, g in enumerate(p["picks"]):
            nf = g["missing"] or ""
            if g["missing_names"]:
                nf = "%s (%s)" % (g["missing"], g["missing_names"])
            rows.append([i + 1, g["bill_no"] + (" (U)" if g["urgent"] else ""), g["items"],
                         g["picked"] if g["picked"] is not None else "—", nf, g["took_at"], g["due_at"],
                         g["done_at"] or "—", g["allowed"], g["taken"] or "—", g["result"],
                         g["points"] if g["points"] is not None else "—"])
            if g["outcome"] == "late":
                red.append(i)
        story.append(_table(["#", "Bill", "Took", "Picked", "Not found", "Took at", "Normal time ends", "Done at",
                             "Time given", "Time taken", "Result", "Points"], rows,
                            [8 * mm, 30 * mm, 12 * mm, 14 * mm, W - 217 * mm, 22 * mm, 26 * mm, 22 * mm, 18 * mm, 18 * mm, 26 * mm, 15 * mm],
                            red_rows=red))
        if p["events"]:
            story.append(Paragraph("Late / waiting log", H2))
            story.append(_table(["Time", "What", "Bill", "Minutes", "Detail"],
                                [[e["at"], e["label"], e["bill"], e["minutes"] if e["minutes"] is not None else "", e["detail"]] for e in p["events"]],
                                [26 * mm, 28 * mm, 34 * mm, 18 * mm, W - 106 * mm],
                                red_rows=[i for i, e in enumerate(p["events"]) if e["kind"] == "late"]))
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()
