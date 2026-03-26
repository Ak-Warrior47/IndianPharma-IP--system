import io, logging
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.piecharts import Pie
from datetime import date

logger = logging.getLogger(__name__)

def calculate_kra_grade(picked, missed, boxes, staff_type='picker'):
    total = picked + missed
    acc   = (picked / total * 100) if total > 0 else 0
    if staff_type == 'picker':
        if acc >= 98 and boxes >= 15: return 'ELITE',        'Exceptional pick accuracy & CS handling. Gold Standard.'
        if acc >= 95:                 return 'PROFICIENT',   'Meets standard pharma pick accuracy requirements.'
        if acc >= 88:                 return 'SATISFACTORY', 'Acceptable. Focus on reducing missed picks.'
        return                               'RE-TRAINING',  'Pick accuracy below safety threshold. Intervention required.'
    else:
        if acc >= 97 and boxes >= 10: return 'ELITE',        'Exceptional verification accuracy. Zero-error standard met.'
        if acc >= 94:                 return 'PROFICIENT',   'Good check accuracy. Minor improvements remain.'
        if acc >= 87:                 return 'SATISFACTORY', 'Acceptable check rate. Increase error detection.'
        return                               'RE-TRAINING',  'Verification accuracy below threshold. Re-training needed.'

def calculate_efficiency_score(staff_type, pick_acc, pick_speed, check_acc, check_time, checked):
    try:
        if staff_type == 'checker':
            ck_speed  = (checked / check_time) if check_time > 0 else 0
            return round(min(100, (check_acc / 100 * 70) + (min(ck_speed / 150, 1) * 30)), 1)
        return round(min(100, (pick_acc / 100 * 60) + (min(pick_speed / 200, 1) * 40)), 1)
    except Exception:
        return 0.0

def _make_pie(labels, vals, hex_colors, title, w=180, h=150):
    d   = Drawing(w, h)
    pie = Pie()
    pie.x, pie.y    = 25, 18
    pie.width = pie.height = 100
    pie.data         = [max(v, 0.01) for v in vals]
    pie.labels       = [f'{l}' for l in labels]
    pie.simpleLabels = False
    pie.sideLabels   = True
    pie.sideLabelsOffset = 0.1
    pie.slices.strokeWidth  = 0.5
    pie.slices.strokeColor  = colors.white
    for i, c in enumerate(hex_colors):
        pie.slices[i].fillColor = colors.HexColor(c)
    d.add(pie)
    d.add(String(w/2, h-10, title, fontSize=8, fontName='Helvetica-Bold',
                 fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
    return d

def _make_bars(vals, labels, title, w=350, h=110, max_val=100):
    d    = Drawing(w, h)
    d.add(String(w/2, h-8, title, fontSize=8, fontName='Helvetica-Bold',
                 fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
    bar_w   = 52
    gap     = 18
    x0      = 30
    area_h  = h - 38
    grays   = ['#0f172a','#374151','#6b7280','#9ca3af']
    for i, (v, lbl) in enumerate(zip(vals, labels)):
        safe_v = max(v or 0, 0)
        bh = max(int(safe_v / max(max_val, 1) * area_h), 2)
        x  = x0 + i * (bar_w + gap)
        d.add(Rect(x, 22, bar_w, bh, fillColor=colors.HexColor(grays[i]), strokeColor=None))
        d.add(String(x + bar_w/2, 22 + bh + 3, str(safe_v),
                     fontSize=7, fontName='Helvetica-Bold',
                     fillColor=colors.HexColor('#0f172a'), textAnchor='middle'))
        d.add(String(x + bar_w/2, 8, lbl,
                     fontSize=6.5, fillColor=colors.HexColor('#64748b'), textAnchor='middle'))
    return d

def generate_visual_pdf(emp_name, payload):
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                rightMargin=1.8*cm, leftMargin=1.8*cm,
                                topMargin=1.8*cm,   bottomMargin=1.8*cm)
        S   = getSampleStyleSheet()

        def ps(name, **kw):
            return ParagraphStyle(name, parent=S['Normal'], **kw)

        title_st = ps('T', fontSize=17, fontName='Helvetica-Bold', alignment=TA_CENTER,
                      textColor=colors.HexColor('#0f172a'), spaceAfter=2)
        sub_st   = ps('S', fontSize=9, alignment=TA_CENTER,
                      textColor=colors.HexColor('#64748b'), spaceAfter=2)
        h2_st    = ps('H2', fontSize=11, fontName='Helvetica-Bold',
                      textColor=colors.HexColor('#0f172a'), spaceBefore=10, spaceAfter=4)
        body_st  = ps('B', fontSize=9, textColor=colors.HexColor('#1e293b'), spaceAfter=4, leading=13)

        all_s   = payload.get('all_stats')
        d_s     = payload.get('day_stats')
        w_s     = payload.get('week_stats')
        m_s     = payload.get('month_stats')
        stype   = payload.get('staff_type', 'picker')
        entries = payload.get('all_entries', [])

        if not all_s:
            doc.build([Paragraph('No data available.', body_st)])
            buf.seek(0)
            return buf

        grade     = all_s['grade']
        grade_clr = {'ELITE':'#166534','PROFICIENT':'#1d4ed8','SATISFACTORY':'#b45309','RE-TRAINING':'#991b1b'}.get(grade,'#0f172a')
        grade_st  = ps('GR', fontSize=17, fontName='Helvetica-Bold', alignment=TA_CENTER,
                       textColor=colors.HexColor(grade_clr))

        elems = []
        elems += [
            Paragraph('INDIAN PHARMACEUTICALS IP', title_st),
            Paragraph('Official KRA Performance Analysis Report', sub_st),
            Paragraph(f"Specialist: {emp_name.upper()} | Role: {stype.title()} | Generated: {date.today():%d %B %Y}", sub_st),
            Spacer(1, 0.2*cm),
            HRFlowable(width='100%', thickness=2, color=colors.HexColor('#0f172a')),
            Spacer(1, 0.3*cm),
            Paragraph(f'Overall KRA Grade: {grade}', grade_st),
            Paragraph(f"Efficiency Score: {all_s['eff_score']}/100",
                      ps('EF', fontSize=12, fontName='Helvetica-Bold', alignment=TA_CENTER,
                         textColor=colors.HexColor('#374151'))),
            Paragraph(all_s['feedback'],
                      ps('FB', fontSize=9, alignment=TA_CENTER, textColor=colors.HexColor('#64748b'), spaceAfter=6)),
            Spacer(1, 0.3*cm),
            HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')),
            Spacer(1, 0.3*cm),
        ]

        # Period comparison table
        elems.append(Paragraph('PERIOD COMPARISON', h2_st))
        def sv(s, k): return str(s[k]) if s and k in s else '—'

        if stype == 'picker':
            rows_data = [
                ['Metric',           'Today',                    'This Week',                'This Month',               'All-Time'],
                ['Pick Accuracy',    sv(d_s,'pick_acc')+'%',     sv(w_s,'pick_acc')+'%',     sv(m_s,'pick_acc')+'%',     sv(all_s,'pick_acc')+'%'],
                ['Items Picked',     sv(d_s,'tp'),               sv(w_s,'tp'),               sv(m_s,'tp'),               sv(all_s,'tp')],
                ['Items Missed',     sv(d_s,'tm'),               sv(w_s,'tm'),               sv(m_s,'tm'),               sv(all_s,'tm')],
                ['Pick Speed/hr',    sv(d_s,'pick_speed'),       sv(w_s,'pick_speed'),       sv(m_s,'pick_speed'),       sv(all_s,'pick_speed')],
                ['Bills',            sv(d_s,'tb'),               sv(w_s,'tb'),               sv(m_s,'tb'),               sv(all_s,'tb')],
                ['CS in Purchase',   sv(d_s,'tbx'),              sv(w_s,'tbx'),              sv(m_s,'tbx'),              sv(all_s,'tbx')],
                ['Sweep Hours',      sv(d_s,'ts'),               sv(w_s,'ts'),               sv(m_s,'ts'),               sv(all_s,'ts')],
                ['Eff. Score',       sv(d_s,'eff_score'),        sv(w_s,'eff_score'),        sv(m_s,'eff_score'),        sv(all_s,'eff_score')],
                ['Potential Items',  sv(d_s,'potential_items'),  sv(w_s,'potential_items'),  sv(m_s,'potential_items'),  sv(all_s,'potential_items')],
                ['Potential Eff.',   sv(d_s,'potential_eff'),    sv(w_s,'potential_eff'),    sv(m_s,'potential_eff'),    sv(all_s,'potential_eff')],
            ]
        else:
            rows_data = [
                ['Metric',           'Today',                    'This Week',                'This Month',               'All-Time'],
                ['Pick Accuracy',    sv(d_s,'pick_acc')+'%',     sv(w_s,'pick_acc')+'%',     sv(m_s,'pick_acc')+'%',     sv(all_s,'pick_acc')+'%'],
                ['Error Detection',  sv(d_s,'check_acc')+'%',    sv(w_s,'check_acc')+'%',    sv(m_s,'check_acc')+'%',    sv(all_s,'check_acc')+'%'],
                ['Items Checked',    sv(d_s,'tck'),              sv(w_s,'tck'),              sv(m_s,'tck'),              sv(all_s,'tck')],
                ['Errors Found',     sv(d_s,'ter'),              sv(w_s,'ter'),              sv(m_s,'ter'),              sv(all_s,'ter')],
                ['Check Speed/hr',   sv(d_s,'ck_speed'),         sv(w_s,'ck_speed'),         sv(m_s,'ck_speed'),         sv(all_s,'ck_speed')],
                ['Items Picked',     sv(d_s,'tp'),               sv(w_s,'tp'),               sv(m_s,'tp'),               sv(all_s,'tp')],
                ['CS in Purchase',   sv(d_s,'tbx'),              sv(w_s,'tbx'),              sv(m_s,'tbx'),              sv(all_s,'tbx')],
                ['Eff. Score',       sv(d_s,'eff_score'),        sv(w_s,'eff_score'),        sv(m_s,'eff_score'),        sv(all_s,'eff_score')],
                ['Potential Items',  sv(d_s,'potential_items'),  sv(w_s,'potential_items'),  sv(m_s,'potential_items'),  sv(all_s,'potential_items')],
                ['Potential Eff.',   sv(d_s,'potential_eff'),    sv(w_s,'potential_eff'),    sv(m_s,'potential_eff'),    sv(all_s,'potential_eff')],
            ]

        ct = Table(rows_data, colWidths=[3.5*cm, 3*cm, 3*cm, 3*cm, 3*cm])
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
            ('BACKGROUND',    (0,-2),(-1,-1), colors.HexColor('#eff6ff')),
        ]))
        elems += [ct, Spacer(1, 0.4*cm)]

        # Pie charts
        elems.append(Paragraph('PERFORMANCE BREAKDOWN CHARTS', h2_st))
        tp, tm = all_s['tp'], all_s['tm']

        pie_row = []
        pie_row.append(_make_pie(['Picked','Missed'], [tp, tm], ['#0f172a','#e2e8f0'], 'Pick vs Miss'))
        cur_eff = all_s['eff_score']
        pie_row.append(_make_pie(['Current','Gap'], [cur_eff, 100-cur_eff], ['#0f172a','#e2e8f0'], 'Eff. vs Potential'))
        if stype == 'checker':
            pie_row.append(_make_pie(['Found','Missed'], [all_s['ter'], max(all_s['tck']-all_s['ter'],0)],
                                     ['#374151','#cbd5e1'], 'Error Detection'))
        else:
            pot_i = all_s['potential_items']
            pie_row.append(_make_pie(['Actual','Unreached'], [tp, max(pot_i-tp,0)],
                                     ['#374151','#cbd5e1'], 'Actual vs Potential'))

        pt = Table([pie_row])
        pt.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
        elems += [pt, Spacer(1, 0.3*cm)]

        # Bar charts
        elems.append(Paragraph('PERIOD TREND — ACCURACY & EFFICIENCY', h2_st))
        acc_v  = [d_s['pick_acc'] if d_s else 0, w_s['pick_acc'] if w_s else 0,
                  m_s['pick_acc'] if m_s else 0, all_s['pick_acc']]
        eff_v  = [d_s['eff_score'] if d_s else 0, w_s['eff_score'] if w_s else 0,
                  m_s['eff_score'] if m_s else 0, all_s['eff_score']]
        spd_v  = [d_s['pick_speed'] if d_s else 0, w_s['pick_speed'] if w_s else 0,
                  m_s['pick_speed'] if m_s else 0, all_s['pick_speed']]
        lbls   = ['Today','Week','Month','All']

        bar_tbl = Table([
            [_make_bars(acc_v, lbls, 'Pick Accuracy % by Period', max_val=100)],
            [_make_bars(eff_v, lbls, 'Efficiency Score by Period', max_val=100)],
            [_make_bars(spd_v, lbls, 'Pick Speed / hr by Period',
                        max_val=max(max(spd_v, default=1)*1.3, 50))],
        ])
        bar_tbl.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        elems += [bar_tbl, Spacer(1, 0.3*cm)]

        # Daily log
        if entries:
            elems += [HRFlowable(width='100%',thickness=0.5,color=colors.HexColor('#e2e8f0')),
                      Spacer(1,0.2*cm), Paragraph('DAILY ENTRY LOG', h2_st)]
            if stype == 'picker':
                log = [['Date','Bills','Picked','Missed','Accuracy','Speed/hr','CS','Sweep hrs']]
                for e in entries:
                    log.append([str(e.entry_date),e.bills,e.picked,e.missed,
                                 f'{e.accuracy}%',f'{e.pick_speed}',e.boxes,e.sweep])
                cws = [2.5*cm,1.5*cm,1.8*cm,1.8*cm,2.2*cm,2.2*cm,1.5*cm,2*cm]
            else:
                log = [['Date','Picked','Missed','Acc%','Checked','Errors','Err%','Chk hrs']]
                for e in entries:
                    log.append([str(e.entry_date),e.picked,e.missed,
                                 f'{e.accuracy}%',e.checked,e.errors_found,
                                 f'{e.check_rate}%',e.check_time])
                cws = [2.5*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,1.8*cm,2*cm]

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
                ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
            ]))
            elems += [lt, Spacer(1,0.4*cm)]

        elems += [
            HRFlowable(width='100%',thickness=2,color=colors.HexColor('#0f172a')),
            Spacer(1,0.2*cm),
            Paragraph('Confidential — Indian Pharmaceuticals IP System v5. Internal use only.',
                      ps('FT',fontSize=7,alignment=TA_CENTER,textColor=colors.HexColor('#94a3b8')))
        ]

        doc.build(elems)
        buf.seek(0)
        return buf

    except Exception as e:
        logger.error(f'PDF generation error: {e}')
        buf = io.BytesIO()
        from reportlab.platypus import SimpleDocTemplate, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
        doc2 = SimpleDocTemplate(buf, pagesize=A4)
        doc2.build([Paragraph(f'Report generation error: {e}', getSampleStyleSheet()['Normal'])])
        buf.seek(0)
        return buf
