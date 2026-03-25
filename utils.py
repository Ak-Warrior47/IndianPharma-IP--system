import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from datetime import date


def calculate_kra_grade(picked, missed, cs_purchase):
    """Calculates professional KRA grade based on Pharma IP standards."""
    total = picked + missed
    acc   = (picked / total * 100) if total > 0 else 0

    if acc >= 98 and cs_purchase >= 15:
        return "ELITE", "Exceptional clinical accuracy and purchase handling. IP Gold Standard."
    if acc >= 95:
        return "PROFICIENT", "Meeting standard operating procedures for pharmaceuticals."
    if acc >= 85:
        return "SATISFACTORY", "Acceptable performance. Focus on reducing missed items."
    return "RE-TRAINING REQUIRED", "Quality threshold breach. Accuracy below safety levels."


def generate_visual_pdf(emp_name, data):
    """Generates the official KRA Performance Analysis Document."""
    buffer = io.BytesIO()
    doc    = SimpleDocTemplate(
        buffer,
        pagesize     = A4,
        rightMargin  = 2 * cm,
        leftMargin   = 2 * cm,
        topMargin    = 2 * cm,
        bottomMargin = 2 * cm,
    )

    grade, feedback = calculate_kra_grade(
        data['picked'],
        data['missed'],
        data['cs_purchase']
    )

    total   = data['picked'] + data['missed']
    acc_pct = round((data['picked'] / total * 100), 1) if total > 0 else 0.0

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'Title', parent=styles['Title'],
        fontSize=20, spaceAfter=4, textColor=colors.HexColor('#0f172a'),
        alignment=TA_CENTER, fontName='Helvetica-Bold'
    )
    sub_style = ParagraphStyle(
        'Sub', parent=styles['Normal'],
        fontSize=10, textColor=colors.HexColor('#64748b'),
        alignment=TA_CENTER, spaceAfter=2
    )
    label_style = ParagraphStyle(
        'Label', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#64748b'),
        fontName='Helvetica-Bold', spaceAfter=2
    )
    body_style = ParagraphStyle(
        'Body', parent=styles['Normal'],
        fontSize=10, textColor=colors.HexColor('#1e293b'),
        spaceAfter=6, leading=14
    )

    grade_color = {
        'ELITE'                : colors.HexColor('#166534'),
        'PROFICIENT'           : colors.HexColor('#1e40af'),
        'SATISFACTORY'         : colors.HexColor('#92400e'),
        'RE-TRAINING REQUIRED' : colors.HexColor('#991b1b'),
    }.get(grade, colors.black)

    grade_style = ParagraphStyle(
        'Grade', parent=styles['Normal'],
        fontSize=18, fontName='Helvetica-Bold',
        textColor=grade_color, alignment=TA_CENTER, spaceAfter=4
    )

    elements = []

    # Header
    elements.append(Paragraph("INDIAN PHARMACEUTICALS IP", title_style))
    elements.append(Paragraph("Official KRA Performance Analysis Report", sub_style))
    elements.append(Paragraph(f"Generated: {date.today().strftime('%d %B %Y')}", sub_style))
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f172a')))
    elements.append(Spacer(1, 0.4 * cm))

    # Employee & Grade
    elements.append(Paragraph(f"Specialist: {emp_name.upper()}", label_style))
    elements.append(Paragraph(f"KRA Grade: {grade}", grade_style))
    elements.append(Paragraph(f"Accuracy Score: {acc_pct}%", grade_style))
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(f"<b>Managerial Assessment:</b> {feedback}", body_style))
    elements.append(Spacer(1, 0.4 * cm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#e2e8f0')))
    elements.append(Spacer(1, 0.4 * cm))

    # KPI Summary Table
    elements.append(Paragraph("KPI PARAMETER SUMMARY", label_style))
    elements.append(Spacer(1, 0.2 * cm))

    table_data = [
        ['KPI Parameter',          'Value'],
        ['Sales Bills Processed',   str(data['bills'])],
        ['Items Picked',            str(data['picked'])],
        ['Items Missed',            str(data['missed'])],
        ['Total Items Handled',     str(total)],
        ['Pick Accuracy',           f"{acc_pct}%"],
        ['CS Opened in Purchase',   f"{data['cs_purchase']} boxes"],
        ['Total Sweeping Time',     f"{data['sweep_hours']} hours"],
    ]

    t = Table(table_data, colWidths=[11 * cm, 5 * cm])
    t.setStyle(TableStyle([
        ('BACKGROUND',  (0, 0), (-1, 0),  colors.HexColor('#0f172a')),
        ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',    (0, 0), (-1, 0),  10),
        ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME',    (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',    (0, 1), (-1, -1), 10),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f8fafc'), colors.white]),
        ('GRID',        (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING',  (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BACKGROUND',  (0, 5), (-1, 5),  colors.HexColor('#f0fdf4')),  # accuracy row highlight
    ]))
    elements.append(t)
    elements.append(Spacer(1, 0.5 * cm))

    # Per-entry log if available
    entries = data.get('entries', [])
    if entries:
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#e2e8f0')))
        elements.append(Spacer(1, 0.4 * cm))
        elements.append(Paragraph("DAILY ENTRY LOG", label_style))
        elements.append(Spacer(1, 0.2 * cm))

        log_data = [['Date', 'Bills', 'Picked', 'Missed', 'Accuracy', 'Sweep (hrs)']]
        for e in entries:
            log_data.append([
                str(e.entry_date),
                str(e.bills),
                str(e.picked),
                str(e.missed),
                f"{e.accuracy}%",
                str(e.sweep),
            ])

        lt = Table(log_data, colWidths=[3.5*cm, 2*cm, 2.5*cm, 2.5*cm, 3*cm, 3*cm])
        lt.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0),  colors.HexColor('#0f172a')),
            ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
            ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
            ('FONTSIZE',    (0, 0), (-1, -1), 8),
            ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f8fafc'), colors.white]),
            ('GRID',        (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('TOPPADDING',  (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        elements.append(lt)

    elements.append(Spacer(1, 0.6 * cm))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#0f172a')))
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(
        "This document is auto-generated by the Indian Pharmaceuticals IP System. "
        "Confidential — for internal use only.",
        ParagraphStyle('Footer', parent=styles['Normal'],
                       fontSize=8, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer
