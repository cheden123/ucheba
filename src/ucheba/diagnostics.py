"""Локальный технический журнал без текста лекций и реквизитов доступа."""

from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
import platform
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile

from .queue import Job, QueueStore

EVENTS = {
    "session_start": "Запуск приложения",
    "job_start": "Запущено задание",
    "job_failed": "Задание завершилось ошибкой",
    "job_succeeded": "Задание выполнено",
    "job_cancelled": "Задание отменено",
    "speaker_candidate": "Предложен вероятный преподаватель",
    "speaker_unresolved": "Недостаточно признаков для выбора преподавателя",
    "speaker_warning": "Автоматическое разделение голосов не завершено",
    "key_check_ok": "OpenRouter принял ключ",
    "key_check_failed": "Проверка ключа OpenRouter завершилась ошибкой",
    "update_check_ok": "Проверка обновлений завершена",
    "update_check_failed": "Проверка обновлений завершилась ошибкой",
    "update_ready": "Установщик проверен",
    "update_launched": "Установщик запущен",
}
REASONS = {
    "network": "Сетевой сервис OpenRouter",
    "media": "Медиа или модель Whisper",
    "slides": "Извлечение кадров или OCR",
    "storage": "Файл, папка или хранилище",
    "validation": "Проверка параметров или входных данных",
    "unknown": "Необработанное исключение",
}
_LIMIT = 2000


def classify_failure(error: Exception) -> str:
    from .analysis import OpenRouterError
    from .slides import SlideError
    from .transcription import TranscriptionError

    if isinstance(error, OpenRouterError):
        return "network"
    if isinstance(error, TranscriptionError):
        return "media"
    if isinstance(error, SlideError):
        return "slides"
    if isinstance(error, (OSError, sqlite3.Error)):
        return "storage"
    if isinstance(error, ValueError):
        return "validation"
    return "unknown"


def _table(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS diagnostics "
        "(id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)"
    )


def record_event(
    queue: QueueStore, event: str, *, job: Job | None = None, reason: str | None = None
) -> None:
    if event not in EVENTS:
        return
    detail = ""
    if job is not None:
        identifier = job.id[:12] if re.fullmatch(r"[a-f0-9]{32}", job.id) else "—"
        model = (
            job.model
            if job.model in {"base", "small", "medium", "large-v3", "large-v3-turbo"}
            else "—"
        )
        device = job.device if job.device in {"cpu", "cuda", "auto"} else "—"
        detail = f" · задача {identifier} · Whisper {model} · {device}"
    if reason is not None and reason in REASONS:
        detail += f" · {REASONS[reason]}"
    try:
        with queue._connect() as db:
            _table(db)
            db.execute(
                "INSERT INTO diagnostics(stamp,event,detail) VALUES (?,?,?)",
                (datetime.now().astimezone().isoformat(timespec="seconds"), event, detail),
            )
            db.execute(
                "DELETE FROM diagnostics WHERE id <= "
                "(SELECT id FROM diagnostics ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (_LIMIT,),
            )
    except sqlite3.Error:
        # Ошибка вспомогательного журнала не прерывает работу приложения.
        pass


def export_log(queue: QueueStore, destination: Path) -> None:
    if destination.suffix.lower() != ".txt":
        raise ValueError("Для экспорта журнала выберите файл с расширением .txt.")
    try:
        app_version = version("ucheba-ai")
    except PackageNotFoundError:
        app_version = "из исходников"
    with queue._connect() as db:
        _table(db)
        rows = db.execute(
            "SELECT stamp,event,detail FROM diagnostics ORDER BY id DESC LIMIT ?", (_LIMIT,)
        ).fetchall()
    lines = [
        f"Учёба AI {app_version}",
        f"Платформа: {platform.system()} {platform.release()} ({platform.machine()})",
        "Журнал не содержит ключей, имён файлов, путей и текста лекций.",
        "",
    ]
    lines.extend(
        f"{row['stamp']}  {EVENTS[row['event']]}{row['detail']}"
        for row in reversed(rows)
        if row["event"] in EVENTS
    )
    with NamedTemporaryFile(
        mode="w", encoding="utf-8-sig", dir=destination.parent, prefix=".ucheba-log-", delete=False
    ) as stream:
        pending = Path(stream.name)
        try:
            stream.write("\n".join(lines) + "\n")
        except OSError:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
