import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable, Image)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.shapes import Drawing, Wedge, String, Rect, Circle
from reportlab.graphics import renderPDF
from reportlab.graphics.charts.piecharts import Pie
from datetime import date
import math

# ── Grade logic ──────────────────────────────────
def calculate_kra_grade(picked, missed, boxes, staff_type='picker'):
    total = picked + missed
    acc   = (picked / total * 100) if total > 0 else 0
    if staff_type == 'picker':
        if acc >= 98 and boxes >= 15: return "ELITE",        "Exceptional pick accuracy + CS handling. Gold Standard."
        if acc >= 95:                 return "PROFICIENT",   "Meets standard pharma pick accuracy requirements."
        if acc >= 88:                 return "SATISFACTORY", "Acceptable. Focus on reducing missed picks."
        return                               "RE-TRAINING",  "Pick accuracy below safety threshold. Intervention required."
    else:  # checker
        if acc >= 97 and boxes >= 10: return "ELITE",        "Exceptional verification accuracy. Zero-error standard met."
        if acc >= 94:                 return "PROFICIENT",   "Good check accuracy. Minor improvement areas remain."
        if acc >= 87:                 return "SATISFACTORY", "Acceptable check rate. Increase error detection focus."
        return                               "RE-TRAINING",  "Verification accuracy below threshold. Re-training required."

def calculate_efficiency_score(staff_type, pick_acc, pick_speed, check_acc, check_time, checked):
    if staff_type == 'picker':
        acc_score   = min(pick_acc, 100) / 100 * 60      # 60% weight on accuracy
        speed_score = min(pick_speed / 200, 1) * 40      # 40% weight on speed (200/hr benchmark)
        return round(acc_score + speed_score, 1)
    else:
        a_score = min(check_acc, 100) / 100 * 70         # 70% weight on error detection
        s_score = min(checked / max(check_time,0.1) / 150, 1) * 30  # 30% speed (150/hr benchmark)
        return round(a_score + s_score, 1)

# ── Pie chart drawing ─────────────────────────────
def make_pie_chart(labels, values, colors_list, title, width=200, height=160):
    d   = Drawing(width, height)
    pie = Pie()
    pie.x, pie.y = 30, 20
    pie.width = pie.height = 110
    pie.data       = values
    pie.labels     = [f"{l}\n{v}" for l, v in zip(labels, values)]
    pie.slices.strokeWidth    = 0.5
    pie.slices.strokeColor    = colors.white
    pie.simpleLabels          = False
    pie.sideLabels            = True
    pie.sideLabelsOffset      = 0.08
    for i, c in enumerate(colors_list):
        pie.slices[i].fillColor = colors.HexColor(c)
    d.add(pie)
    d.add(String(width/2, height-12, title,
                 fontSize=8, fontName='Helvetica-Bold',
                 fillColor=colors.HexColor('#0f172a'),
                 textAnchor='middle'))
    return d

# ── Period comparison bar chart ───────────────────
def make_bar_chart(label_vals, title, width=370, height=120, max_val=100):
    d = Drawing(width, height)
    d.add(String(width/2, height-10, title,
                 fontSize=8, fontName='Helvetica-Bold',
                 fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
    bar_w   = 55
    gap     = 25
    x_start = 40
    bar_area= height - 40
    labels  = ['Today', 'Week', 'Month', 'All-Time']
    bcolors = ['#0f172a', '#374151', '#6b7280', '#9ca3af']
    for i, (lbl, val) in enumerate(zip(labels, label_vals)):
        x   = x_start + i*(bar_w + gap)
        bh  = round(val / max(max_val,1) * bar_area) if val else 2
        by  = 25
        d.add(Rect(x, by, bar_w, bh,
                   fillColor=colors.HexColor(bcolors[i]),
                   strokeColor=None))
        d.add(String(x + bar_w/2, by + bh + 3, str(val),
                     fontSize=7, fontName='Helvetica-Bold',
                     fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
        d.add(String(x + bar_w/2, 10, lbl,
                     fontSize=6.5, fontName='Helvetica',
                     fillColor=colors.HexColor('#64748b'), textAnchor='middle'))
    return d

# ── PDF generator ─────────────────────────────────
def generate_visual_pdf(emp_name, payload):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=1.8*cm, leftMargin=1.8*cm,
                            topMargin=1.8*cm,   bottomMargin=1.8*cm)
    s   = getSampleStyleSheet()

    def ps(name, **kw):
        p = ParagraphStyle(name, parent=s['Normal'], **kw)
        return p

    title_st  = ps('T', fontSize=18, fontName='Helvetica-Bold', alignment=TA_CENTER,
                   textColor=colors.HexColor('#0f172a'), spaceAfter=2)
    sub_st    = ps('S', fontSize=9,  alignment=TA_CENTER,
                   textColor=colors.HexColor('#64748b'), spaceAfter=2)
    h2_st     = ps('H2', fontSize=11, fontName='Helvetica-Bold',
                   textColor=colors.HexColor('#0f172a'), spaceBefore=10, spaceAfter=4)
    body_st   = ps('B', fontSize=9,  textColor=colors.HexColor('#1e293b'),
                   spaceAfter=4, leading=13)
    grade_st  = ps('G', fontSize=20, fontName='Helvetica-Bold', alignment=TA_CENTER)

    all_s   = payload.get('all_stats')
    d_s     = payload.get('day_stats')
    w_s     = payload.get('week_stats')
    m_s     = payload.get('month_stats')
    stype   = payload.get('staff_type', 'picker')
    entries = payload.get('all_entries', [])

    if not all_s: return buf

    grade     = all_s['grade']
    grade_clr = {'ELITE':'#166534','PROFICIENT':'#1d4ed8','SATISFACTORY':'#b45309','RE-TRAINING':'#991b1b'}.get(grade,'#0f172a')
    grade_st  = ps('GR', fontSize=18, fontName='Helvetica-Bold', alignment=TA_CENTER,
                   textColor=colors.HexColor(grade_clr))

    elems = []

    # Header
    elems += [
        Paragraph("INDIAN PHARMACEUTICALS IP", title_st),
        Paragraph("Official KRA Performance Analysis Report", sub_st),
        Paragraph(f"Specialist: {emp_name.upper()} | Role: {stype.title()} | Generated: {date.today():%d %B %Y}", sub_st),
        Spacer(1, 0.2*cm),
        HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f172a')),
        Spacer(1, 0.3*cm),
        Paragraph(f"Overall KRA Grade: {grade}", grade_st),
        Paragraph(f"Efficiency Score: {all_s['eff_score']}/100", ps('ES', fontSize=13,
            fontName='Helvetica-Bold', alignment=TA_CENTER,
            textColor=colors.HexColor('#374151'))),
        Paragraph(all_s['feedback'], ps('FB', fontSize=9, alignment=TA_CENTER,
            textColor=colors.HexColor('#64748b'), spaceAfter=6)),
        Spacer(1, 0.3*cm),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#e2e8f0')),
        Spacer(1, 0.3*cm),
    ]

    # ── Period Comparison Table ─────────────────────
    elems.append(Paragraph("PERIOD COMPARISON", h2_st))
    def sv(stats, key): return str(stats[key]) if stats else '—'

    if stype == 'picker':
        comp_data = [
            ['Metric',            'Today',                    'This Week',                'This Month',               'All-Time'],
            ['Pick Accuracy',     sv(d_s,'pick_acc')+'%',     sv(w_s,'pick_acc')+'%',     sv(m_s,'pick_acc')+'%',     sv(all_s,'pick_acc')+'%'],
            ['Items Picked',      sv(d_s,'tp'),               sv(w_s,'tp'),               sv(m_s,'tp'),               sv(all_s,'tp')],
            ['Items Missed',      sv(d_s,'tm'),               sv(w_s,'tm'),               sv(m_s,'tm'),               sv(all_s,'tm')],
            ['Pick Speed (hr)',   sv(d_s,'pick_speed'),       sv(w_s,'pick_speed'),       sv(m_s,'pick_speed'),       sv(all_s,'pick_speed')],
            ['Bills Processed',   sv(d_s,'tb'),               sv(w_s,'tb'),               sv(m_s,'tb'),               sv(all_s,'tb')],
            ['CS in Purchase',    sv(d_s,'tbx'),              sv(w_s,'tbx'),              sv(m_s,'tbx'),              sv(all_s,'tbx')],
            ['Sweep Hours',       sv(d_s,'ts'),               sv(w_s,'ts'),               sv(m_s,'ts'),               sv(all_s,'ts')],
            ['Efficiency Score',  sv(d_s,'eff_score'),        sv(w_s,'eff_score'),        sv(m_s,'eff_score'),        sv(all_s,'eff_score')],
            ['Potential Items',   sv(d_s,'potential_items'),  sv(w_s,'potential_items'),  sv(m_s,'potential_items'),  sv(all_s,'potential_items')],
            ['Potential Eff.',    sv(d_s,'potential_eff'),    sv(w_s,'potential_eff'),    sv(m_s,'potential_eff'),    sv(all_s,'potential_eff')],
        ]
    else:
        comp_data = [
            ['Metric',            'Today',                    'This Week',                'This Month',               'All-Time'],
            ['Pick Accuracy',     sv(d_s,'pick_acc')+'%',     sv(w_s,'pick_acc')+'%',     sv(m_s,'pick_acc')+'%',     sv(all_s,'pick_acc')+'%'],
            ['Error Detection',   sv(d_s,'check_acc')+'%',    sv(w_s,'check_acc')+'%',    sv(m_s,'check_acc')+'%',    sv(all_s,'check_acc')+'%'],
            ['Items Checked',     sv(d_s,'tck'),              sv(w_s,'tck'),              sv(m_s,'tck'),              sv(all_s,'tck')],
            ['Errors Found',      sv(d_s,'ter'),              sv(w_s,'ter'),              sv(m_s,'ter'),              sv(all_s,'ter')],
            ['Check Speed (hr)',  sv(d_s,'ck_speed'),         sv(w_s,'ck_speed'),         sv(m_s,'ck_speed'),         sv(all_s,'ck_speed')],
            ['Items Picked',      sv(d_s,'tp'),               sv(w_s,'tp'),               sv(m_s,'tp'),               sv(all_s,'tp')],
            ['CS in Purchase',    sv(d_s,'tbx'),              sv(w_s,'tbx'),              sv(m_s,'tbx'),              sv(all_s,'tbx')],
            ['Efficiency Score',  sv(d_s,'eff_score'),        sv(w_s,'eff_score'),        sv(m_s,'eff_score'),        sv(all_s,'eff_score')],
            ['Potential Items',   sv(d_s,'potential_items'),  sv(w_s,'potential_items'),  sv(m_s,'potential_items'),  sv(all_s,'potential_items')],
            ['Potential Eff.',    sv(d_s,'potential_eff'),    sv(w_s,'potential_eff'),    sv(m_s,'potential_eff'),    sv(all_s,'potential_eff')],
        ]

    cw = [3.5*cm, 3*cm, 3*cm, 3*cm, 3*cm]
    ct = Table(comp_data, colWidths=cw)
    ct.setStyle(TableStyle([
        ('BACKGROUND',    (0,0),(-1,0),  colors.HexColor('#0f172a')),
        ('TEXTCOLOR',     (0,0),(-1,0),  colors.white),
        ('FONTNAME',      (0,0),(-1,0),  'Helvetica-Bold'),
        ('FONTNAME',      (0,1),(-1,-1), 'Helvetica'),
        ('FONTSIZE',      (0,0),(-1,-1), 8),
        ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
        ('ROWBACKGROUNDS',(0,1),(-1,-1), [colors.HexColor('#f8fafc'), colors.white]),
        ('GRID',          (0,0),(-1,-1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING',    (0,0),(-1,-1), 5),
        ('BOTTOMPADDING', (0,0),(-1,-1), 5),
        ('BACKGROUND',    (0,-2),(-1,-1),colors.HexColor('#eff6ff')),  # potential rows highlight
        ('FONTNAME',      (0,-2),(0,-1), 'Helvetica-Bold'),
    ]))
    elems += [ct, Spacer(1, 0.4*cm)]

    # ── Pie Charts ──────────────────────────────────
    elems.append(Paragraph("PERFORMANCE BREAKDOWN (ALL-TIME PIES)", h2_st))
    tp, tm = all_s['tp'], all_s['tm']
    pie_table_data = [[]]

    p1 = make_pie_chart(
        ['Picked','Missed'], [max(tp,1), max(tm,1)],
        ['#0f172a','#e2e8f0'], 'Pick vs Miss')
    pie_table_data[0].append(p1)

    if stype == 'picker':
        cur_eff  = all_s['eff_score']
        gap_eff  = round(100 - cur_eff, 1)
        p2 = make_pie_chart(
            ['Current Eff.','Potential Gap'],
            [max(cur_eff,1), max(gap_eff,1)],
            ['#0f172a','#e2e8f0'], 'Efficiency vs Potential')
        pie_table_data[0].append(p2)
        cur_items = all_s['tp']
        pot_items = max(all_s['potential_items'] - cur_items, 0)
        p3 = make_pie_chart(
            ['Actual','Unreached Potential'],
            [max(cur_items,1), max(pot_items,1)],
            ['#374151','#cbd5e1'], 'Items: Actual vs Potential')
        pie_table_data[0].append(p3)
    else:
        tck, ter = all_s['tck'], all_s['ter']
        p2 = make_pie_chart(
            ['Errors Found','Missed Errors'],
            [max(ter,1), max(tck-ter,1)],
            ['#0f172a','#e2e8f0'], 'Check Accuracy')
        pie_table_data[0].append(p2)
        cur_eff = all_s['eff_score']
        p3 = make_pie_chart(
            ['Current Eff.','Potential Gap'],
            [max(cur_eff,1), max(100-cur_eff,1)],
            ['#374151','#cbd5e1'], 'Efficiency vs Potential')
        pie_table_data[0].append(p3)

    pt = Table(pie_table_data)
    pt.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    elems += [pt, Spacer(1, 0.3*cm)]

    # ── Bar Charts: period comparison ───────────────
    elems.append(Paragraph("PERIOD TREND CHARTS", h2_st))
    def gv(s, k, default=0): return s[k] if s else default

    acc_vals   = [gv(d_s,'pick_acc'), gv(w_s,'pick_acc'), gv(m_s,'pick_acc'), gv(all_s,'pick_acc')]
    eff_vals   = [gv(d_s,'eff_score'), gv(w_s,'eff_score'), gv(m_s,'eff_score'), gv(all_s,'eff_score')]
    speed_vals = [gv(d_s,'pick_speed'), gv(w_s,'pick_speed'), gv(m_s,'pick_speed'), gv(all_s,'pick_speed')]

    bar1 = make_bar_chart(acc_vals,   "Pick Accuracy % (Day/Week/Month/All)", max_val=100)
    bar2 = make_bar_chart(eff_vals,   "Efficiency Score (Day/Week/Month/All)", max_val=100)
    bar3 = make_bar_chart(speed_vals, "Pick Speed per Hour (Day/Week/Month/All)",
                          max_val=max(max(speed_vals,default=1)*1.2, 50))

    bar_tbl = Table([[bar1],[bar2],[bar3]])
    bar_tbl.setStyle(TableStyle([
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    elems += [bar_tbl, Spacer(1, 0.3*cm)]

    # ── Daily Entry Log ──────────────────────────────
    if entries:
        elems += [HRFlowable(width="100%",thickness=0.5,color=colors.HexColor('#e2e8f0')),
                  Spacer(1,0.2*cm),
                  Paragraph("DAILY ENTRY LOG", h2_st)]
        if stype == 'picker':
            log = [['Date','Bills','Picked','Missed','Accuracy','Speed/hr','CS','Sweep hrs']]
            for e in entries:
                log.append([str(e.entry_date), e.bills, e.picked, e.missed,
                             f"{e.accuracy}%", f"{e.pick_speed}", e.boxes, e.sweep])
            cws = [2.5*cm,1.5*cm,1.8*cm,1.8*cm,2.2*cm,2.2*cm,1.5*cm,2*cm]
        else:
            log = [['Date','Picked','Missed','Accuracy','Checked','Errors','Err%','Check hrs']]
            for e in entries:
                log.append([str(e.entry_date), e.picked, e.missed,
                             f"{e.accuracy}%", e.checked, e.errors_found,
                             f"{e.check_rate}%", e.check_time])
            cws = [2.5*cm,1.8*cm,1.8*cm,2.2*cm,1.8*cm,1.8*cm,1.8*cm,2*cm]
        lt = Table(log, colWidths=cws)
        lt.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0f172a')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTNAME',(0,1),(-1,-1),'Helvetica'),
            ('FONTSIZE',(0,0),(-1,-1),7.5),
            ('ALIGN',(0,0),(-1,-1),'CENTER'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f8fafc'),colors.white]),
            ('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#e2e8f0')),
            ('TOPPADDING',(0,0),(-1,-1),4),
            ('BOTTOMPADDING',(0,0),(-1,-1),4),
        ]))
        elems += [lt, Spacer(1,0.4*cm)]

    # Footer
    elems += [
        HRFlowable(width="100%",thickness=2,color=colors.HexColor('#0f172a')),
        Spacer(1,0.2*cm),
        Paragraph("Confidential — Indian Pharmaceuticals IP System v4. For internal use only.",
                  ps('FT', fontSize=7, alignment=TA_CENTER, textColor=colors.HexColor('#94a3b8')))
    ]

    doc.build(elems)
    buf.seek(0)
    return buf
