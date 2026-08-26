from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Event, TaskPhoto, User
from .storage import get_object_bytes


def _font_name() -> str:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("SSBotArchive", candidate))
            return "SSBotArchive"
        except Exception:
            continue
    return "Helvetica"


def build_event_archive_pdf(event: Event, participants: list[User], tasks: list[dict]) -> bytes:
    """Build a printable event archive; Telegram history is intentionally excluded."""
    output = io.BytesIO()
    font = _font_name()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ArchiveTitle", parent=styles["Title"], fontName=font, fontSize=19)
    body = ParagraphStyle("ArchiveBody", parent=styles["BodyText"], fontName=font, leading=14)
    heading = ParagraphStyle("ArchiveHeading", parent=styles["Heading2"], fontName=font)
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=f"Архив — {event.title}",
    )
    story = [Paragraph("Архив события", title), Spacer(1, 5 * mm)]
    story.append(Paragraph(f"<b>{event.title}</b>", heading))
    details = [
        ["Начало", event.starts_at.isoformat()],
        ["Окончание", event.ends_at.isoformat() if event.ends_at else "Не указано"],
        ["Бюджет", str(event.budget) if event.budget is not None else "Не указан"],
        [
            "Хранить до",
            event.retention_delete_at.isoformat()
            if event.retention_delete_at
            else "Будет определено после закрытия",
        ],
    ]
    table = Table(details, colWidths=(43 * mm, 125 * mm))
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E8EEF8")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C4D8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([table, Spacer(1, 5 * mm), Paragraph("Участники", heading)])
    participant_text = ", ".join(user.full_name or str(user.telegram_id) for user in participants)
    story.extend([Paragraph(participant_text or "Нет участников", body), Spacer(1, 4 * mm)])
    story.append(Paragraph("Задачи и отчёты", heading))
    for task in tasks:
        report = task.get("report")
        lines = [
            f"<b>{task['title']}</b> - {task['status']}",
            f"Срок: {task['deadline'].isoformat()}",
        ]
        if report:
            lines.extend(
                [
                    f"Отчёт: {report['comment'] or 'Без комментария'}",
                    f"Фотографий: {report['photo_count']}",
                ]
            )
        story.extend([Paragraph("<br/>".join(lines), body), Spacer(1, 3 * mm)])
    document.build(story)
    return output.getvalue()


def build_photo_zip(photos: Iterable[TaskPhoto]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for photo in photos:
            filename = PurePosixPath(photo.object_key).name
            archive.writestr(filename, get_object_bytes(photo.object_key))
    return output.getvalue()
