from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from docx import Document
from docx.document import Document as WordDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.styles.style import ParagraphStyle

from .models import Analysis, Segment

if TYPE_CHECKING:
    from .slides import Slide

INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def safe_text(value: str) -> str:
    return INVALID_XML.sub("", value)


def timestamp(seconds: float, *, ms: bool = False) -> str:
    whole, milliseconds = divmod(round(seconds * 1000), 1000)
    minutes, second = divmod(whole, 60)
    hour, minute = divmod(minutes, 60)
    value = f"{hour:02}:{minute:02}:{second:02}"
    return f"{value},{milliseconds:03}" if ms else value


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def academic_document() -> WordDocument:
    document = Document()
    document.core_properties.language = "ru-RU"
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)
    section.top_margin = section.bottom_margin = Cm(2)
    normal = cast(ParagraphStyle, document.styles["Normal"])
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(1.25)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for name in ("Title", "Heading 1", "Heading 2", "List Bullet", "List Number"):
        style = cast(ParagraphStyle, document.styles[name])
        style.font.name = "Times New Roman"
        style.paragraph_format.first_line_indent = Cm(0)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    page_number = OxmlElement("w:fldSimple")
    page_number.set(qn("w:instr"), "PAGE")
    footer._p.append(page_number)
    return document


def export(
    folder: Path,
    title: str,
    segments: list[Segment],
    analysis: Analysis | None,
) -> None:
    title = safe_text(title)
    full = academic_document()
    full.add_heading(title, 0)
    full.add_heading("Полный текст лекции", 1)
    transcript = []
    subtitles: list[str] = []
    for item in segments:
        prefix = f"[{timestamp(item.start)}] " if item.end > item.start else ""
        who = f"{item.speaker}: " if item.speaker else ""
        line = prefix + who + item.text
        transcript.append(line)
        full.add_paragraph(safe_text(line))
        if item.end > item.start:
            subtitles.append(
                f"{len(subtitles) + 1}\n{timestamp(item.start, ms=True)} --> "
                f"{timestamp(item.end, ms=True)}\n{item.text.strip()}"
            )
    full.save(str(folder / "01_Полный_текст_лекции.docx"))
    (folder / "Исходная_расшифровка.txt").write_text("\n".join(transcript), encoding="utf-8")
    if subtitles:
        (folder / "Расшифровка_с_метками.srt").write_text(
            "\n\n".join(subtitles) + "\n", encoding="utf-8"
        )
    write_json(folder / "transcript.json", [item.model_dump(mode="json") for item in segments])
    if analysis is None:
        return
    summary = academic_document()
    summary.add_heading(title, 0)
    for heading, items in (
        ("Краткий конспект", analysis.summary),
        ("Подробный конспект", analysis.details),
    ):
        summary.add_heading(heading, 1)
        for point in items:
            summary.add_paragraph(safe_text(point), style="List Bullet")
    summary.save(str(folder / "02_Конспект_лекции.docx"))
    tasks = academic_document()
    tasks.add_heading(title, 0)
    tasks.add_heading("Задания преподавателя", 1)
    tasks.add_paragraph(
        "Сверьте найденные фрагменты с записью, чтобы подтвердить задание и говорящего."
    )
    if not analysis.assignments:
        tasks.add_paragraph("Анализ не выделил фрагментов с заданиями.")
    for task in analysis.assignments:
        tasks.add_heading(safe_text(task.title), 2)
        tasks.add_paragraph(safe_text(task.requirements))
        tasks.add_paragraph("Срок сдачи: " + safe_text(task.deadline or "не указан"))
        timed = task.end > task.start or task.source_kind == "slide"
        label = f"[{timestamp(task.start)}] " if timed else ""
        if task.source_kind == "slide":
            label += "Текст слайда (OCR): "
        speaker = f"{task.speaker}: " if task.speaker else ""
        tasks.add_paragraph(safe_text(f"{label}{speaker}«{task.quote}»"))
        if task.plan:
            tasks.add_paragraph("Предлагаемый план выполнения")
            for step in task.plan:
                tasks.add_paragraph(safe_text(step), style="List Number")
    tasks.save(str(folder / "03_Задания_преподавателя.docx"))
    data = analysis.model_dump(mode="json")
    write_json(folder / "analysis.json", data)
    write_json(folder / "tasks.json", data["assignments"])


def export_slides(folder: Path, title: str, slides: list[Slide]) -> None:
    document = academic_document()
    document.add_heading(safe_text(title), 0)
    document.add_heading("Кадры лекции", 1)
    document.add_paragraph(
        "Кадры выбраны автоматически. Текст OCR может содержать ошибки или текст других окон."
    )
    frames = folder / "frames"
    frames.mkdir()
    index = []
    for number, slide in enumerate(slides, start=1):
        path = frames / f"{number:03d}{slide.image.suffix.lower()}"
        shutil.copyfile(slide.image, path)
        document.add_heading(timestamp(slide.start), 2)
        document.add_picture(str(path), width=Cm(15))
        if slide.text.strip():
            document.add_paragraph(safe_text(slide.text))
        index.append(
            {
                "start": slide.start,
                "image": path.relative_to(folder).as_posix(),
                "text": slide.text,
            }
        )
    document.save(str(folder / "04_Слайды_лекции.docx"))
    write_json(folder / "slides.json", index)
