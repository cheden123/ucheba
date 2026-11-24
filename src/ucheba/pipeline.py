from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from .documents import export, write_json
from .imports import IMPORTS, MEDIA, VIDEO, load_transcript
from .models import Analysis, Segment
from .queue import Job, source_identity

if TYPE_CHECKING:
    from .slides import Slide


def _cache(job: Job) -> Path:
    if job.state_dir is not None:
        return job.state_dir / job.id
    if job.cloud:
        profile = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return profile / "UchebaAI" / "checkpoints" / job.id
    return job.output / ".ucheba-checkpoints" / job.id


def partial_result(job: Job) -> Path | None:
    folder = _cache(job) / "Расшифровка"
    return folder if (folder / "metadata.json").is_file() else None


def _check_source_consent(job: Job) -> None:
    if job.cloud and job.source_identity != source_identity(job.source):
        raise ValueError(
            "Исходная запись изменилась. Добавьте её в очередь заново и подтвердите передачу текста."
        )


def _publish(
    folder: Path,
    job: Job,
    segments: list[Segment],
    analysis: Analysis | None,
    cancelled: Callable[[], bool],
    slides: list[Slide] | None = None,
    candidate: str | None = None,
) -> None:
    folder.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(mkdtemp(prefix=f".ucheba-{job.id}-", dir=folder.parent))
    try:
        export(temp, job.source.stem, segments, analysis, template=job.template)
        if slides:
            from .documents import export_slides

            export_slides(temp, job.source.stem, slides, template=job.template)
        with (temp / "transcript.json").open("rb") as stream:
            transcript_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        write_json(
            temp / "metadata.json",
            {
                "source_name": job.source.name,
                "job_id": job.id,
                "cloud": analysis is not None,
                "lecturer_candidate": candidate,
                "transcript_sha256": transcript_sha256,
                "created": datetime.now().astimezone().isoformat(),
            },
        )
        if cancelled():
            raise InterruptedError("Обработка отменена")
        temp.rename(folder)
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def process(
    job: Job,
    key: str | None,
    progress: Callable[[str], None],
    cancelled: Callable[[], bool],
) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", job.source.stem)[:60].rstrip(" .")
    folder = job.output / f"{datetime.now():%Y-%m-%d}_{name}_{job.id}"
    previous = next(job.output.glob(f"*_{job.id}"), None)
    if previous is not None and (previous / "metadata.json").is_file():
        return previous
    if not job.source.is_file():
        raise ValueError("Исходный файл недоступен. Проверьте путь и подключение к папке.")
    _check_source_consent(job)
    if cancelled():
        raise InterruptedError("Обработка отменена")
    has_optional_steps = job.cloud or job.speaker_model is not None or job.slides
    partial = partial_result(job) if has_optional_steps else None
    extension = job.source.suffix.lower()
    if partial is not None:
        progress("Чтение сохранённой расшифровки")
        transcript = partial / "transcript.json"
        metadata = json.loads((partial / "metadata.json").read_text(encoding="utf-8"))
        with transcript.open("rb") as stream:
            raw = stream.read(50 * 1024 * 1024 + 1)
        if len(raw) > 50 * 1024 * 1024:
            raise ValueError("Размер сохранённой расшифровки превышает 50 МБ.")
        transcript_sha256 = hashlib.sha256(raw).hexdigest()
        if transcript_sha256 != metadata.get("transcript_sha256"):
            raise ValueError("Сохранённая расшифровка изменилась. Добавьте её как новое задание.")
        segments = TypeAdapter(list[Segment]).validate_json(raw)
    elif extension in IMPORTS:
        progress("Чтение расшифровки")
        segments = load_transcript(job.source)
    elif extension in MEDIA:
        from .transcription import TranscriptionError, transcribe

        cache = _cache(job)
        cache.mkdir(parents=True, exist_ok=True)
        cpu_checkpoint = cache / "transcription-cpu.json"
        selected_device = "cpu" if job.device == "auto" and cpu_checkpoint.exists() else job.device
        checkpoint = (
            cpu_checkpoint
            if selected_device == "cpu" and job.device == "auto"
            else (cache / "transcription.json")
        )
        try:
            segments = transcribe(
                job.source,
                checkpoint,
                model=job.model,
                device=selected_device,
                progress=progress,
                cancelled=cancelled,
            )
        except TranscriptionError as exc:
            if selected_device != "auto" or not isinstance(exc.__cause__, (RuntimeError, OSError)):
                raise
            progress("Ускорение недоступно. Распознавание продолжится на CPU.")
            segments = transcribe(
                job.source,
                cpu_checkpoint,
                model=job.model,
                device="cpu",
                progress=progress,
                cancelled=cancelled,
            )
    else:
        raise ValueError("Формат файла не поддерживается")
    if not segments:
        raise ValueError("В записи не обнаружена речь")
    if cancelled():
        raise InterruptedError("Обработка отменена")
    candidate = None
    if job.speaker_model is not None and extension in MEDIA:
        from .diarization import diarize, suggest_lecturer

        if partial is None or not any(segment.speaker for segment in segments):
            try:
                speakers = diarize(
                    job.source, segments, job.speaker_model, progress=progress, cancelled=cancelled
                )
            except InterruptedError:
                raise
            except (RuntimeError, OSError, ValueError) as exc:
                if partial is None:
                    _publish(_cache(job) / "Расшифровка", job, segments, None, cancelled)
                raise ValueError(str(exc)) from exc
            segments, candidate = list(speakers.segments), speakers.candidate
        else:
            candidate = suggest_lecturer(segments)

    if has_optional_steps and partial is None:
        progress("Сохранение расшифровки")
        _publish(_cache(job) / "Расшифровка", job, segments, None, cancelled, candidate=candidate)
    if job.cloud and not key:
        raise ValueError("Добавьте ключ OpenRouter в настройках для облачного анализа")
    with TemporaryDirectory(prefix="ucheba-slides-") as work:
        slides = []
        if job.slides and extension in VIDEO:
            from .slides import extract_slides

            executable = shutil.which("tesseract")
            if not executable:
                installed = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                tesseract = installed / "Tesseract-OCR" / "tesseract.exe"
                executable = str(tesseract) if tesseract.is_file() else None
            if not executable:
                progress(
                    "Tesseract не найден. Сохраняются изображения слайдов без распознавания текста."
                )
            slides = extract_slides(
                job.source,
                Path(work) / "frames",
                progress=progress,
                cancelled=cancelled,
                tesseract=Path(executable) if executable else None,
            )
        analysis = None
        if job.cloud and key:
            from .analysis import analyze

            _check_source_consent(job)
            progress("Анализ расшифровки в OpenRouter")
            analysis = analyze(
                segments,
                key,
                progress=progress,
                cancelled=cancelled,
                cache_dir=_cache(job) / "analysis",
                slides=[(slide.start, slide.text) for slide in slides if slide.text.strip()],
            )
        progress("Создание документов Word")
        _publish(folder, job, segments, analysis, cancelled, slides, candidate)
    return folder
