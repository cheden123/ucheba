import hashlib
import json
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from .documents import export, write_json
from .imports import IMPORTS, MEDIA, load_transcript
from .models import Analysis, Segment
from .queue import Job, source_identity


def _cache(job: Job) -> Path:
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
) -> None:
    folder.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(mkdtemp(prefix=f".ucheba-{job.id}-", dir=folder.parent))
    try:
        export(temp, job.source.stem, segments, analysis)
        with (temp / "transcript.json").open("rb") as stream:
            transcript_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        write_json(
            temp / "metadata.json",
            {
                "source_name": job.source.name,
                "job_id": job.id,
                "cloud": analysis is not None,
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
    partial = partial_result(job) if job.cloud else None
    extension = job.source.suffix.lower()
    if partial is not None:
        progress("Чтение сохранённой расшифровки")
        transcript = partial / "transcript.json"
        metadata = json.loads((partial / "metadata.json").read_text(encoding="utf-8"))
        with transcript.open("rb") as stream:
            transcript_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        if transcript_sha256 != metadata.get("transcript_sha256"):
            raise ValueError("Сохранённая расшифровка изменилась. Добавьте её как новое задание.")
        segments = load_transcript(transcript)
    elif extension in IMPORTS:
        progress("Чтение расшифровки")
        segments = load_transcript(job.source)
    elif extension in MEDIA:
        from .transcription import transcribe

        cache = _cache(job)
        cache.mkdir(parents=True, exist_ok=True)
        segments = transcribe(
            job.source,
            cache / "transcription.json",
            model=job.model,
            device=job.device,
            progress=progress,
            cancelled=cancelled,
        )
    else:
        raise ValueError("Формат файла не поддерживается")
    if not segments:
        raise ValueError("В записи не обнаружена речь")
    if cancelled():
        raise InterruptedError("Обработка отменена")
    analysis = None
    if job.cloud:
        from .analysis import analyze

        _check_source_consent(job)
        if partial is None:
            progress("Сохранение расшифровки")
            _publish(_cache(job) / "Расшифровка", job, segments, None, cancelled)
        if not key:
            raise ValueError("Добавьте ключ OpenRouter в настройках для облачного анализа")
        progress("Анализ расшифровки в OpenRouter")
        analysis = analyze(
            segments,
            key,
            progress=progress,
            cancelled=cancelled,
            cache_dir=_cache(job) / "analysis",
        )
    progress("Создание документов Word")
    _publish(folder, job, segments, analysis, cancelled)
    return folder
