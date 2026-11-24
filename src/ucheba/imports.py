import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from pydantic import TypeAdapter

from .models import Segment

MEDIA = frozenset(
    ".mp4 .mkv .avi .mov .webm .wmv .m4v .mpeg .ts "
    ".mp3 .wav .m4a .aac .flac .ogg .opus .wma".split()
)
VIDEO = frozenset(".mp4 .mkv .avi .mov .webm .wmv .m4v .mpeg .ts".split())
IMPORTS = frozenset({".txt", ".docx", ".srt", ".vtt", ".json"})
TIMECODE = r"(?:(?:\d{2,}):)?[0-5]\d:[0-5]\d[.,]\d{3}"
CUE = re.compile(rf"^({TIMECODE})\s+-->\s+({TIMECODE})(?:\s+.*)?$")


def _seconds(timecode: str) -> float:
    seconds = 0.0
    for part in timecode.replace(",", ".").split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def load_transcript(source: Path) -> list[Segment]:
    if source.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Размер файла расшифровки превышает 50 МБ")
    extension = source.suffix.lower()
    if extension == ".json":
        segments = TypeAdapter(list[Segment]).validate_json(source.read_bytes())
    elif extension == ".docx":
        try:
            with ZipFile(source) as archive:
                if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError("Размер распакованного документа превышает 100 МБ")
        except BadZipFile as exc:
            raise ValueError("Файл не является исправным документом Word") from exc
        segments = [
            Segment(start=0, end=0, text=p.text.strip())
            for p in Document(str(source)).paragraphs
            if p.text.strip()
        ]
    else:
        try:
            content = source.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Сохраните текстовый файл или субтитры в кодировке UTF-8") from exc
        if extension in {".srt", ".vtt"}:
            segments = []
            for block in re.split(r"\n[ \t]*\n", content):
                lines = block.strip().splitlines()
                if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
                    continue
                for index, line in enumerate(lines[:2]):
                    match = CUE.fullmatch(line.strip())
                    if match:
                        segments.append(
                            Segment(
                                start=_seconds(match[1]),
                                end=_seconds(match[2]),
                                text="\n".join(lines[index + 1 :]).strip(),
                            )
                        )
                        break
                else:
                    raise ValueError("Не удалось прочитать временную метку субтитров")
        else:
            segments = [
                Segment(start=0, end=0, text=line.strip())
                for line in content.splitlines()
                if line.strip()
            ]
    if not segments or any(item.end < item.start or not item.text.strip() for item in segments):
        raise ValueError("Расшифровка пуста или содержит некорректные фрагменты")
    return sorted(segments, key=lambda item: item.start)
