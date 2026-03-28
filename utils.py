import io, logging
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.piecharts import Pie
from datetime import date

logger = logging.getLogger(__name__)

DARK  = "#0f172a"
BLUE  = "#1d4ed8"
GRAY  = "#64748b"
LGRAY = "#e2e8f0"
WHITE = "#ffffff"
BG    = "#f8fafc"

def _ps(S, name, **kw):
    return ParagraphStyle(name, parent=S["Normal"], **kw)

def _make_pie(labels, vals, hex_colors, title, w=175, h=145):
    try:
        d   = Drawing(w, h)
        pie = Pie()
        pie.x, pie.y = 22, 16
        pie.width = pie.height = 96
        pie.data         = [max(float(v), 0.01) for v in vals]
        pie.simpleLabels = False
        pie.sideLabels   = True
        pie.sideLabelsOffset = 0.12
        pie.slices.strokeWidth  = 0.5
        pie.slices.strokeColor  = colors.HexColor(WHITE)
        for i, c in enumerate(hex_colors):
            pie.slices[i].fillColor = colors.HexColor(c)
        d.add(pie)
        d.add(String(w/2, h-9, title, fontSize=7.5, fontName="Helvetica-Bold",
                     fillColor=colors.HexColor(DARK), textAnchor="middle"))
        # Legend
        for i, (lbl, v) in enumerate(zip(labels, vals)):
            d.add(Rect(4, h-24-i*12, 8, 8, fillColor=colors.HexColor(hex_colors[i]), strokeColor=None))
            d.add(String(16, h-19-i*12, f"{lbl}: {v}", fontSize=6.5,
                         fillColor=colors.HexColor(GRAY), textAnchor="start"))
        return d
    except Exception as e:
        logger.error(f"Pie error: {e}")
        d2 = Drawing(w, h)
        d2.add(String(w/2, h/2, "Chart error", fontSize=8, textAnchor="middle",
                      fillColor=colors.HexColor(GRAY)))
        return d2

def _make_bars(vals, labels, title, w=350, h=110, max_val=100):
    try:
        d = Drawing(w, h)
        d.add(String(w/2, h-8, title, fontSize=7.5, fontName="Helvetica-Bold",
                     fillColor=colors.HexColor(DARK), textAnchor="middle"))
        bar_w = 52; gap = 16; x0 = 28; area_h = h-40
        grays = [DARK, "#374151", "#6b7280", "#9ca3af"]
        for i, (v, lbl) in enumerate(zip(vals, labels)):
            sv = max(float(v) if v else 0, 0)
            bh = max(int(sv / max(float(max_val), 1) * area_h), 2)
            x  = x0 + i * (bar_w + gap)
            d.add(Rect(x, 22, bar_w, bh, fillColor=colors.HexColor(grays[min(i,3)]), strokeColor=None))
            d.add(String(x+bar_w/2, 22+bh+3, str(round(sv,1)),
                         fontSize=7, fontName="Helvetica-Bold",
                         fillColor=colors.HexColor(DARK), textAnchor="middle"))
            d.add(String(x+bar_w/2, 8, lbl, fontSize=6.5,
                         fillColor=colors.HexColor(GRAY), textAnchor="middle"))
        return d
    except Exception as e:
        logger.error(f"Bar error: {e}")
        d2 = Drawing(w, h)
        d2.add(String(w/2, h/2, "Chart error", fontSize=8, textAnchor="middle",
                      fillColor=colors.HexColor(GRAY)))
        return d2

def generate_visual_pdf(emp_name, payload):
    _buf = io.BytesIO()
    try:
        _doc = SimpleDocTemplate(_buf, pagesize=A4,
                                 rightMargin=1.8*cm, leftMargin=1.8*cm,
                                 topMargin=1.8*cm,   bottomMargin=1.8*cm)
        _S   = getSampleStyleSheet()
        def _p(n, **kw): return _ps(_S, n, **kw)

        title_st = _p("T1", fontSize=17, fontName="Helvetica-Bold", alignment=TA_CENTER,
                       textColor=colors.HexColor(BLUE), spaceAfter=2)
        sub_st   = _p("T2", fontSize=9, alignment=TA_CENTER,
                       textColor=colors.HexColor(GRAY), spaceAfter=2)
        h2_st    = _p("H2", fontSize=10, fontName="Helvetica-Bold",
                       textColor=colors.HexColor(DARK), spaceBefore=10, spaceAfter=4,
                       borderPad=4)
        body_st  = _p("B1", fontSize=9, textColor=colors.HexColor(DARK), spaceAfter=4, leading=13)
        formula_st = _p("FM", fontSize=8, textColor=colors.HexColor(GRAY),
                         fontName="Helvetica-Oblique", spaceAfter=2, leading=12)

        all_s   = payload.get("all_stats")
        d_s     = payload.get("day_stats")
        w_s     = payload.get("week_stats")
        m_s     = payload.get("month_stats")
        stype   = payload.get("staff_type", "picker")
        entries = payload.get("all_entries", [])

        elems = []

        if not all_s:
            elems.append(Paragraph("No performance data available yet.", body_st))
            _doc.build(elems)
            _buf.seek(0)
            return _buf

        grade     = all_s.get("grade","N/A")
        grade_clr = {"ELITE":BLUE,"PROFICIENT":"#1d4ed8","SATISFACTORY":"#b45309",
                     "RE-TRAINING":"#991b1b"}.get(grade, DARK)
        grade_st  = _p("GS", fontSize=16, fontName="Helvetica-Bold", alignment=TA_CENTER,
                        textColor=colors.HexColor(grade_clr))

        # ── HEADER ──
        elems += [
            Paragraph("INDIAN PHARMACEUTICALS IP", title_st),
            Paragraph("Official KRA Performance Analysis Report", sub_st),
            Paragraph(f"Specialist: {emp_name.upper()}  |  Role: {stype.title()}  |  Generated: {date.today():%d %B %Y}", sub_st),
            Spacer(1, 0.2*cm),
            HRFlowable(width="100%", thickness=2, color=colors.HexColor(BLUE)),
            Spacer(1, 0.3*cm),
            Paragraph(f"KRA Grade: {grade}", grade_st),
            Paragraph(f"Efficiency Score: {all_s.get("eff_score",0)}/100  |  Consistency: {all_s.get("consistency",0)}%  |  Trend: {all_s.get("trend","stable").upper()}",
                      _p("EF", fontSize=11, fontName="Helvetica-Bold", alignment=TA_CENTER,
                         textColor=colors.HexColor(DARK))),
            Paragraph(all_s.get("feedback",""), _p("FB", fontSize=9, alignment=TA_CENTER,
                      textColor=colors.HexColor(GRAY), spaceAfter=6)),
            Spacer(1, 0.25*cm),
            HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(LGRAY)),
            Spacer(1, 0.25*cm),
        ]

        # ── FORMULA BOX ──
        elems.append(Paragraph("CALCULATION METHODOLOGY", h2_st))
        if stype == "picker":
            formula_text = ("Efficiency = (Pick Acc/100)*60 + min(Speed/200,1)*40 [max 100]<br/>"
                "Benchmark: 98% acc + 200 items/hr = 100 eff. Potential = 200*sweep_hrs.<br/>"
                "Potential_eff = eff + (98-acc)*0.5 + (200-speed)*0.1 [cap 100]<br/>"
                "Consistency = 100 - (StdDev of daily accuracy * 2)")
        else:
            formula_text = ("Efficiency = (Error_Det/100)*70 + min(Chk_Speed/150,1)*30 [max 100]<br/>"
                "Benchmark: 100% error detection + 150 checks/hr = 100 eff.<br/>"
                "Potential = 150*check_hours. Potential_eff = eff + (100-check_acc)*0.5 [cap 100]<br/>"
                "Consistency = 100 - (StdDev of daily accuracy * 2)")
        fbox_data = [[Paragraph(formula_text.replace("\n", "<br/>"), formula_st)]]
        fbox = Table(fbox_data, colWidths=[16*cm])
        fbox.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f0f9ff")),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor(BLUE)),
            ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
            ("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),
        ]))
        elems += [fbox, Spacer(1, 0.3*cm)]

        # ── PERIOD COMPARISON TABLE ──
        elems.append(Paragraph("PERIOD-BY-PERIOD COMPARISON", h2_st))
        def sv(s, k, sfx=""): return (str(s[k])+sfx) if s and s.get(k) is not None else "—"

        if stype == "picker":
            rows_data = [
                ["Metric","Today","This Week","This Month","All-Time"],
                ["Pick Accuracy",    sv(d_s,"pick_acc","%"), sv(w_s,"pick_acc","%"), sv(m_s,"pick_acc","%"), sv(all_s,"pick_acc","%")],
                ["Items Picked",     sv(d_s,"tp"),     sv(w_s,"tp"),     sv(m_s,"tp"),     sv(all_s,"tp")],
                ["Items Missed",     sv(d_s,"tm"),     sv(w_s,"tm"),     sv(m_s,"tm"),     sv(all_s,"tm")],
                ["Pick Speed /hr",   sv(d_s,"pick_speed"), sv(w_s,"pick_speed"), sv(m_s,"pick_speed"), sv(all_s,"pick_speed")],
                ["Bills Processed",  sv(d_s,"tb"),     sv(w_s,"tb"),     sv(m_s,"tb"),     sv(all_s,"tb")],
                ["CS in Purchase",   sv(d_s,"tbx"),    sv(w_s,"tbx"),    sv(m_s,"tbx"),    sv(all_s,"tbx")],
                ["Sweep Hours",      sv(d_s,"ts"),     sv(w_s,"ts"),     sv(m_s,"ts"),     sv(all_s,"ts")],
                ["Efficiency Score", sv(d_s,"eff_score"), sv(w_s,"eff_score"), sv(m_s,"eff_score"), sv(all_s,"eff_score")],
                ["Consistency %",    sv(d_s,"consistency"), sv(w_s,"consistency"), sv(m_s,"consistency"), sv(all_s,"consistency")],
                ["Potential Items",  sv(d_s,"potential_items"), sv(w_s,"potential_items"), sv(m_s,"potential_items"), sv(all_s,"potential_items")],
                ["Potential Eff.",   sv(d_s,"potential_eff"), sv(w_s,"potential_eff"), sv(m_s,"potential_eff"), sv(all_s,"potential_eff")],
                ["Items Gap",        sv(d_s,"gap_items"), sv(w_s,"gap_items"), sv(m_s,"gap_items"), sv(all_s,"gap_items")],
            ]
        else:
            rows_data = [
                ["Metric","Today","This Week","This Month","All-Time"],
                ["Pick Accuracy",    sv(d_s,"pick_acc","%"), sv(w_s,"pick_acc","%"), sv(m_s,"pick_acc","%"), sv(all_s,"pick_acc","%")],
                ["Error Detection",  sv(d_s,"check_acc","%"), sv(w_s,"check_acc","%"), sv(m_s,"check_acc","%"), sv(all_s,"check_acc","%")],
                ["Items Checked",    sv(d_s,"tck"), sv(w_s,"tck"), sv(m_s,"tck"), sv(all_s,"tck")],
                ["Errors Found",     sv(d_s,"ter"), sv(w_s,"ter"), sv(m_s,"ter"), sv(all_s,"ter")],
                ["Check Speed /hr",  sv(d_s,"ck_speed"), sv(w_s,"ck_speed"), sv(m_s,"ck_speed"), sv(all_s,"ck_speed")],
                ["Items Picked",     sv(d_s,"tp"), sv(w_s,"tp"), sv(m_s,"tp"), sv(all_s,"tp")],
                ["CS in Purchase",   sv(d_s,"tbx"), sv(w_s,"tbx"), sv(m_s,"tbx"), sv(all_s,"tbx")],
                ["Efficiency Score", sv(d_s,"eff_score"), sv(w_s,"eff_score"), sv(m_s,"eff_score"), sv(all_s,"eff_score")],
                ["Consistency %",    sv(d_s,"consistency"), sv(w_s,"consistency"), sv(m_s,"consistency"), sv(all_s,"consistency")],
                ["Potential Items",  sv(d_s,"potential_items"), sv(w_s,"potential_items"), sv(m_s,"potential_items"), sv(all_s,"potential_items")],
                ["Potential Eff.",   sv(d_s,"potential_eff"), sv(w_s,"potential_eff"), sv(m_s,"potential_eff"), sv(all_s,"potential_eff")],
                ["Items Gap",        sv(d_s,"gap_items"), sv(w_s,"gap_items"), sv(m_s,"gap_items"), sv(all_s,"gap_items")],
            ]

        ct = Table(rows_data, colWidths=[3.8*cm, 2.8*cm, 2.8*cm, 2.8*cm, 2.8*cm])
        ct.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),   colors.HexColor(BLUE)),
            ("TEXTCOLOR",     (0,0),(-1,0),   colors.white),
            ("FONTNAME",      (0,0),(-1,0),   "Helvetica-Bold"),
            ("FONTNAME",      (0,1),(-1,-1),  "Helvetica"),
            ("FONTSIZE",      (0,0),(-1,-1),  8),
            ("ALIGN",         (0,0),(-1,-1),  "CENTER"),
            ("ALIGN",         (0,1),(0,-1),   "LEFT"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),  [colors.HexColor(BG), colors.white]),
            ("GRID",          (0,0),(-1,-1),  0.5, colors.HexColor(LGRAY)),
            ("TOPPADDING",    (0,0),(-1,-1),  5),("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("BACKGROUND",    (0,-3),(-1,-1), colors.HexColor("#eff6ff")),  # potential rows
            ("FONTNAME",      (0,-3),(0,-1),  "Helvetica-Bold"),
        ]))
        elems += [ct, Spacer(1, 0.4*cm)]

        # ── PIE CHARTS ──
        elems.append(Paragraph("PERFORMANCE BREAKDOWN — PIE CHARTS", h2_st))
        tp, tm = all_s.get("tp",0), all_s.get("tm",0)
        cur_eff = all_s.get("eff_score",0)
        p1 = _make_pie(["Picked","Missed"], [tp, tm], [BLUE, LGRAY], "Pick vs Miss")
        p2 = _make_pie(["Current","Gap"], [cur_eff, max(100-cur_eff,0)], [DARK, LGRAY], "Eff. vs Potential")
        if stype == "checker":
            tck, ter = all_s.get("tck",0), all_s.get("ter",0)
            p3 = _make_pie(["Found","Missed Err"], [ter, max(tck-ter,0)], [BLUE, "#bfdbfe"], "Error Detection")
        else:
            pot_i = all_s.get("potential_items",0)
            gap_i = all_s.get("gap_items",0)
            p3 = _make_pie(["Actual","Gap"], [tp, max(gap_i,0)], [BLUE, "#bfdbfe"], "Actual vs Potential")
        pt = Table([[p1, p2, p3]])
        pt.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
        elems += [pt, Spacer(1, 0.3*cm)]

        # ── BAR CHARTS ──
        elems.append(Paragraph("PERIOD TREND CHARTS", h2_st))
        lbls  = ["Today","Week","Month","All"]
        def gv(s, k): return (s[k] if s and s.get(k) is not None else 0)
        acc_v  = [gv(d_s,"pick_acc"), gv(w_s,"pick_acc"), gv(m_s,"pick_acc"), gv(all_s,"pick_acc")]
        eff_v  = [gv(d_s,"eff_score"), gv(w_s,"eff_score"), gv(m_s,"eff_score"), gv(all_s,"eff_score")]
        spd_v  = [gv(d_s,"pick_speed"), gv(w_s,"pick_speed"), gv(m_s,"pick_speed"), gv(all_s,"pick_speed")]
        cons_v = [gv(d_s,"consistency"), gv(w_s,"consistency"), gv(m_s,"consistency"), gv(all_s,"consistency")]

        bt = Table([
            [_make_bars(acc_v,  lbls, "Pick Accuracy % by Period",     max_val=100)],
            [_make_bars(eff_v,  lbls, "Efficiency Score by Period",     max_val=100)],
            [_make_bars(cons_v, lbls, "Consistency Score by Period",    max_val=100)],
            [_make_bars(spd_v,  lbls, "Pick Speed /hr by Period",
                        max_val=max(max([float(v) for v in spd_v if v], default=1)*1.3, 50))],
        ])
        bt.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
        elems += [bt, Spacer(1, 0.3*cm)]

        # ── DAILY LOG ──
        if entries:
            elems += [HRFlowable(width="100%",thickness=0.5,color=colors.HexColor(LGRAY)),
                      Spacer(1,0.2*cm), Paragraph("DAILY ENTRY LOG", h2_st)]
            if stype == "picker":
                log = [["Date","Bills","Picked","Missed","Accuracy","Speed/hr","CS","Sweep hrs"]]
                for e in entries:
                    log.append([str(e.entry_date),e.bills,e.picked,e.missed,
                                 f"{e.accuracy}%",f"{e.pick_speed}",e.boxes,e.sweep])
                cws = [2.5*cm,1.5*cm,1.8*cm,1.8*cm,2.2*cm,2.2*cm,1.5*cm,2*cm]
            else:
                log = [["Date","Picked","Missed","Acc%","Checked","Errors","Err%","Chk hrs"]]
                for e in entries:
                    log.append([str(e.entry_date),e.picked,e.missed,
                                 f"{e.accuracy}%",e.checked,e.errors_found,
                                 f"{e.check_rate}%",e.check_time])
                cws = [2.5*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,2*cm]
            lt = Table(log, colWidths=cws)
            lt.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor(BLUE)),
                ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                ("FONTNAME",(0,1),(-1,-1),"Helvetica"),
                ("FONTSIZE",(0,0),(-1,-1),7.5),
                ("ALIGN",(0,0),(-1,-1),"CENTER"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor(BG),colors.white]),
                ("GRID",(0,0),(-1,-1),0.5,colors.HexColor(LGRAY)),
                ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
            ]))
            elems += [lt, Spacer(1,0.4*cm)]

        elems += [
            HRFlowable(width="100%",thickness=2,color=colors.HexColor(BLUE)),
            Spacer(1,0.2*cm),
            Paragraph("Confidential — Indian Pharmaceuticals IP System v6. Internal use only.",
                      _p("FT",fontSize=7,alignment=TA_CENTER,textColor=colors.HexColor(GRAY)))
        ]

        _doc.build(elems)
        _buf.seek(0)
        return _buf

    except Exception as _e:
        logger.error(f"PDF generation error: {_e}")
        import traceback; traceback.print_exc()
        # Return minimal valid PDF with error message
        _buf2 = io.BytesIO()
        _S2   = getSampleStyleSheet()
        _doc2 = SimpleDocTemplate(_buf2, pagesize=A4)
        _doc2.build([
            Paragraph("INDIAN PHARMACEUTICALS IP", _S2["Title"]),
            Paragraph(f"PDF generation encountered an error: {_e}", _S2["Normal"]),
            Paragraph("Please contact your system administrator.", _S2["Normal"]),
        ])
        _buf2.seek(0)
        return _buf2
