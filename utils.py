import io
import math
from datetime import date
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, 
                                 TableStyle, HRFlowable, Image, PageBreak)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.shapes import Drawing, Rect, String, Circle, Wedge
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics import renderPDF

# ── GRADE LOGIC (FIXED) ──────────────────────────────────────────────────
def calculate_kra_grade(picked, missed, boxes, staff_type='picker'):
    # Fix: Prevent NoneType errors
    picked = picked or 0
    missed = missed or 0
    boxes = boxes or 0
    
    total = picked + missed
    # Fix: Prevent ZeroDivision
    acc = (picked / total * 100) if total > 0 else 0
    
    if staff_type == 'picker':
        if acc >= 98 and boxes >= 15: 
            return "ELITE", "Exceptional pick accuracy + CS handling. Gold Standard."
        elif acc >= 95: 
            return "PROFICIENT", "Meets standard pharma pick accuracy requirements."
        elif acc >= 88: 
            return "SATISFACTORY", "Acceptable. Focus on reducing missed picks."
        else:
            return "RE-TRAINING", "Pick accuracy below safety threshold. Intervention required."
    else:  # checker logic
        if acc >= 97 and boxes >= 10: 
            return "ELITE", "Exceptional verification accuracy. Zero-error standard met."
        elif acc >= 94: 
            return "PROFICIENT", "Good check accuracy. Minor improvement areas remain."
        elif acc >= 87: 
            return "SATISFACTORY", "Acceptable check rate. Increase error detection focus."
        else:
            return "RE-TRAINING", "Verification accuracy below threshold. Re-training required."

# ── EFFICIENCY SCORE (FIXED) ─────────────────────────────────────────────
def calculate_efficiency_score(staff_type, pick_acc, pick_speed, check_acc, check_time, checked):
    # Fix: Safety defaults
    pick_acc = pick_acc or 0
    pick_speed = pick_speed or 0
    check_acc = check_acc or 0
    check_time = max(check_time or 0, 0.1) # Prevent 0 division
    checked = checked or 0

    if staff_type == 'picker':
        acc_score = (min(pick_acc, 100) / 100) * 60
        speed_score = min(pick_speed / 200, 1) * 40
        return round(acc_score + speed_score, 1)
    else:
        a_score = (min(check_acc, 100) / 100) * 70
        s_score = min(checked / check_time / 150, 1) * 30
        return round(a_score + s_score, 1)

# ── CHART DRAWING FUNCTIONS ──────────────────────────────────────────────
def make_pie_chart(labels, values, colors_list, title, width=200, height=160):
    d = Drawing(width, height)
    pie = Pie()
    pie.x = 30
    pie.y = 20
    pie.width = 110
    pie.height = 110
    pie.data = values
    pie.labels = [f"{l}\n{v}" for l, v in zip(labels, values)]
    pie.slices.strokeWidth = 0.5
    pie.slices.strokeColor = colors.white
    pie.simpleLabels = False
    pie.sideLabels = True
    pie.sideLabelsOffset = 0.08
    for i, c in enumerate(colors_list):
        if i < len(pie.slices):
            pie.slices[i].fillColor = colors.HexColor(c)
    d.add(pie)
    d.add(String(width/2, height-12, title, fontSize=8, fontName='Helvetica-Bold', 
                 fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
    return d

def make_bar_chart(label_vals, title, width=370, height=120, max_val=100):
    d = Drawing(width, height)
    d.add(String(width/2, height-10, title, fontSize=8, fontName='Helvetica-Bold', 
                 fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
    
    bar_w = 55
    gap = 25
    x_start = 40
    bar_area = height - 40
    
    labels = ['Today', 'Week', 'Month', 'All-Time']
    bcolors = ['#0f172a', '#374151', '#6b7280', '#9ca3af']
    
    for i, (lbl, val) in enumerate(zip(labels, label_vals)):
        x = x_start + i * (bar_w + gap)
        # Fix: Ensure val isn't None
        v = val or 0
        bh = round(v / max(max_val, 1) * bar_area) if v > 0 else 2
        by = 25
        d.add(Rect(x, by, bar_w, bh, fillColor=colors.HexColor(bcolors[i]), strokeColor=None))
        d.add(String(x + bar_w/2, by + bh + 3, str(v), fontSize=7, fontName='Helvetica-Bold', 
                     fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
        d.add(String(x + bar_w/2, 10, lbl, fontSize=6.5, fontName='Helvetica', 
                     fillColor=colors.HexColor('#64748b'), textAnchor='middle'))
    return d

# ── PDF GENERATOR (FULL VERSION) ──────────────────────────────────────────
def generate_visual_pdf(emp_name, payload):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, 
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()

    def create_style(name, **kw):
        return ParagraphStyle(name, parent=styles['Normal'], **kw)

    # Styles
    title_st = create_style('T', fontSize=20, fontName='Helvetica-Bold', alignment=TA_CENTER, textColor=colors.HexColor('#0f172a'))
    sub_st = create_style('S', fontSize=10, alignment=TA_CENTER, textColor=colors.HexColor('#64748b'), spaceAfter=12)
    h2_st = create_style('H2', fontSize=12, fontName='Helvetica-Bold', textColor=colors.HexColor('#0f172a'), spaceBefore=15, spaceAfter=8)
    label_st = create_style('L', fontSize=9, fontName='Helvetica-Bold', textColor=colors.HexColor('#1e293b'))
    
    # Payload unpacking with safety
    all_s = payload.get('all_stats', {})
    d_s = payload.get('day_stats', {})
    w_s = payload.get('week_stats', {})
    m_s = payload.get('month_stats', {})
    stype = payload.get('staff_type', 'picker')
    entries = payload.get('all_entries', [])
    
    grade = all_s.get('grade', 'N/A')
    grade_clr = {'ELITE':'#166534','PROFICIENT':'#1d4ed8','SATISFACTORY':'#b45309','RE-TRAINING':'#991b1b'}.get(grade,'#0f172a')
    grade_st = create_style('GR', fontSize=22, fontName='Helvetica-Bold', alignment=TA_CENTER, textColor=colors.HexColor(grade_clr))

    elements = []

    # 1. Header Section
    elements.append(Paragraph("INDIAN PHARMACEUTICALS IP", title_st))
    elements.append(Paragraph("Comprehensive KRA Performance Analytics", sub_st))
    elements.append(Paragraph(f"Employee: {emp_name.upper()} | Role: {stype.upper()} | Date: {date.today():%d %b %Y}", sub_st))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f172a'), spaceAfter=10))

    # 2. Executive Summary (Grade & Score)
    elements.append(Paragraph(f"FINAL PERFORMANCE GRADE: {grade}", grade_st))
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(f"Calculated Efficiency: {all_s.get('eff_score', 0)} / 100", create_style('ES', fontSize=14, fontName='Helvetica-Bold', alignment=TA_CENTER)))
    elements.append(Paragraph(f"<i>\"{all_s.get('feedback', 'No feedback available.')}\"</i>", create_style('FB', fontSize=10, alignment=TA_CENTER, textColor=colors.HexColor('#475569'), spaceAfter=20)))

    # 3. Detailed Metrics Table (The 11-row version)
    elements.append(Paragraph("KEY PERFORMANCE INDICATORS (KPI) OVERVIEW", h2_st))
    
    def get_v(s, k): 
        val = s.get(k, '—')
        return str(val) if val is not None else '—'

    if stype == 'picker':
        table_data = [
            ['METRIC DESCRIPTION', 'TODAY', 'WEEKLY', 'MONTHLY', 'CUMULATIVE'],
            ['Pick Accuracy', get_v(d_s,'pick_acc')+'%', get_v(w_s,'pick_acc')+'%', get_v(m_s,'pick_acc')+'%', get_v(all_s,'pick_acc')+'%'],
            ['Total Items Picked', get_v(d_s,'tp'), get_v(w_s,'tp'), get_v(m_s,'tp'), get_v(all_s,'tp')],
            ['Total Items Missed', get_v(d_s,'tm'), get_v(w_s,'tm'), get_v(m_s,'tm'), get_v(all_s,'tm')],
            ['Picking Speed (/hr)', get_v(d_s,'pick_speed'), get_v(w_s,'pick_speed'), get_v(m_s,'pick_speed'), get_v(all_s,'pick_speed')],
            ['Bills Processed', get_v(d_s,'tb'), get_v(w_s,'tb'), get_v(m_s,'tb'), get_v(all_s,'tb')],
            ['CS Handling (Boxes)', get_v(d_s,'tbx'), get_v(w_s,'tbx'), get_v(m_s,'tbx'), get_v(all_s,'tbx')],
            ['Sweep/Helper Hours', get_v(d_s,'ts'), get_v(w_s,'ts'), get_v(m_s,'ts'), get_v(all_s,'ts')],
            ['Efficiency Index', get_v(d_s,'eff_score'), get_v(w_s,'eff_score'), get_v(m_s,'eff_score'), get_v(all_s,'eff_score')],
            ['Potential Item Cap', get_v(d_s,'potential_items'), get_v(w_s,'potential_items'), get_v(m_s,'potential_items'), get_v(all_s,'potential_items')],
            ['Potential Efficiency', get_v(d_s,'potential_eff'), get_v(w_s,'potential_eff'), get_v(m_s,'potential_eff'), get_v(all_s,'potential_eff')],
        ]
    else:
        table_data = [
            ['METRIC DESCRIPTION', 'TODAY', 'WEEKLY', 'MONTHLY', 'CUMULATIVE'],
            ['Pick Accuracy (Gen)', get_v(d_s,'pick_acc')+'%', get_v(w_s,'pick_acc')+'%', get_v(m_s,'pick_acc')+'%', get_v(all_s,'pick_acc')+'%'],
            ['Checking Accuracy', get_v(d_s,'check_acc')+'%', get_v(w_s,'check_acc')+'%', get_v(m_s,'check_acc')+'%', get_v(all_s,'check_acc')+'%'],
            ['Total Items Checked', get_v(d_s,'tck'), get_v(w_s,'tck'), get_v(m_s,'tck'), get_v(all_s,'tck')],
            ['Errors Detected', get_v(d_s,'ter'), get_v(w_s,'ter'), get_v(m_s,'ter'), get_v(all_s,'ter')],
            ['Checking Speed (/hr)', get_v(d_s,'ck_speed'), get_v(w_s,'ck_speed'), get_v(m_s,'ck_speed'), get_v(all_s,'ck_speed')],
            ['Items Picked (Self)', get_v(d_s,'tp'), get_v(w_s,'tp'), get_v(m_s,'tp'), get_v(all_s,'tp')],
            ['CS/Verification Box', get_v(d_s,'tbx'), get_v(w_s,'tbx'), get_v(m_s,'tbx'), get_v(all_s,'tbx')],
            ['Efficiency Index', get_v(d_s,'eff_score'), get_v(w_s,'eff_score'), get_v(m_s,'eff_score'), get_v(all_s,'eff_score')],
            ['Potential Check Cap', get_v(d_s,'potential_items'), get_v(w_s,'potential_items'), get_v(m_s,'potential_items'), get_v(all_s,'potential_items')],
            ['Potential Efficiency', get_v(d_s,'potential_eff'), get_v(w_s,'potential_eff'), get_v(m_s,'potential_eff'), get_v(all_s,'potential_eff')],
        ]

    t = Table(table_data, colWidths=[5.5*cm, 3*cm, 3*cm, 3*cm, 3.5*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f172a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f1f5f9')]),
        ('TEXTCOLOR', (0, 1), (0, -1), colors.HexColor('#0f172a')),
        ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 0.8*cm))

    # 4. Visual Charts Section
    elements.append(Paragraph("PERFORMANCE VISUALIZATION", h2_st))
    
    # Pie Charts Row
    tp = all_s.get('tp') or 0
    tm = all_s.get('tm') or 0
    eff = all_s.get('eff_score') or 0
    
    p1 = make_pie_chart(['Picked', 'Missed'], [max(tp, 1), max(tm, 1)], ['#0f172a', '#e2e8f0'], 'Pick Ratio')
    p2 = make_pie_chart(['Efficiency', 'Gap'], [max(eff, 1), max(100-eff, 1)], ['#1e293b', '#f1f5f9'], 'Efficiency vs Goal')
    
    chart_table = Table([[p1, p2]], colWidths=[9*cm, 9*cm])
    chart_table.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER')]))
    elements.append(chart_table)
    
    # Bar Charts
    def gv(s, k): return s.get(k, 0) or 0
    acc_data = [gv(d_s,'pick_acc'), gv(w_s,'pick_acc'), gv(m_s,'pick_acc'), gv(all_s,'pick_acc')]
    eff_data = [gv(d_s,'eff_score'), gv(w_s,'eff_score'), gv(m_s,'eff_score'), gv(all_s,'eff_score')]
    
    elements.append(make_bar_chart(acc_data, "Accuracy Trend (%)"))
    elements.append(Spacer(1, 0.5*cm))
    elements.append(make_bar_chart(eff_data, "Efficiency Score Trend"))
    
    # 5. Daily Activity Log (Page 2)
    if entries:
        elements.append(PageBreak())
        elements.append(Paragraph("DETAILED DAILY ACTIVITY LOG", h2_st))
        
        if stype == 'picker':
            log_h = ['Date', 'Bills', 'Picked', 'Missed', 'Acc%', 'Speed', 'Boxes', 'Sweep']
            log_data = [log_h]
            for e in entries:
                log_data.append([str(e.entry_date), e.bills, e.picked, e.missed, f"{e.accuracy}%", e.pick_speed, e.boxes, e.sweep])
        else:
            log_h = ['Date', 'Picked', 'Missed', 'Acc%', 'Checked', 'Errors', 'Err%', 'Ck Hrs']
            log_data = [log_h]
            for e in entries:
                log_data.append([str(e.entry_date), e.picked, e.missed, f"{e.accuracy}%", e.checked, e.errors_found, f"{e.check_rate}%", e.check_time])
        
        lt = Table(log_data, colWidths=[2.2*cm, 1.8*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2*cm])
        lt.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#334155')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
            ('FONTSIZE', (0, 0), (-1, -1), 7.5),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        elements.append(lt)

    # Footer
    elements.append(Spacer(1, 1*cm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#94a3b8')))
    elements.append(Paragraph("System Version: IP-V4.2026 | Confidential - For Internal Pharmacy Use Only", 
                              create_style('FT', fontSize=7, alignment=TA_CENTER, textColor=colors.HexColor('#94a3b8'))))

    doc.build(elements)
    buf.seek(0)
    return buf