"""Локальный технический журнал без текста лекций и реквизитов доступа."""

from datetime import datetime
from importlib.metadata import PackageNotFoundError
from json import JSONDecodeError
import platform
from pathlib import Path
import re
from socket import gaierror
import sqlite3
from subprocess import CalledProcessError, TimeoutExpired
from tempfile import NamedTemporaryFile
from zipfile import BadZipFile

import httpx
from keyring import errors as keyring_errors

from .app_version import current_version
from .queue import Job, QueueStore

EVENTS = {
    "session_start": "Запуск приложения",
    "job_start": "Запущено задание",
    "job_failed": "Задание завершилось ошибкой",
    "job_succeeded": "Задание выполнено",
    "job_cancelled": "Задание отменено",
    "stage_start": "Начат этап",
    "stage_succeeded": "Этап завершён",
    "stage_failed": "Ошибка на этапе",
    "stage_note": "Событие на этапе",
    "speaker_candidate": "Предложен вероятный преподаватель",
    "speaker_unresolved": "Недостаточно признаков для выбора преподавателя",
    "speaker_warning": "Автоматическое разделение голосов не завершено",
    "key_check_ok": "OpenRouter принял ключ",
    "key_check_failed": "Проверка ключа OpenRouter завершилась ошибкой",
    "update_check_ok": "Проверка обновлений завершена",
    "update_check_failed": "Проверка обновлений завершилась ошибкой",
    "update_download_failed": "Не удалось подготовить установщик обновления",
    "update_launch_failed": "Не удалось запустить установщик обновления",
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
STAGES = {
    "prepare": "Подготовка задания",
    "read_key": "Чтение ключа OpenRouter",
    "read_transcript": "Чтение готовой расшифровки",
    "probe_media": "Проверка записи",
    "convert": "Подготовка аудио",
    "load_whisper": "Загрузка модели Whisper",
    "transcribe": "Распознавание речи",
    "save_transcript": "Сохранение расшифровки",
    "speakers": "Разделение говорящих",
    "slides": "Извлечение слайдов",
    "analyze": "Анализ лекции",
    "save": "Сохранение результатов",
    "openrouter_key": "Проверка ключа OpenRouter",
    "update_check": "Проверка обновлений",
    "update_download": "Подготовка установщика",
    "update_launch": "Запуск установщика",
}
CODES = {
    "cloud_unavailable": "Облачный анализ недоступен",
    "cuda_fallback": "Распознавание продолжено на CPU",
    "no_speech": "Речь в записи не обнаружена",
}
_LIMIT = 2000
_OS_ERROR_TYPES = (
    OSError,
    BlockingIOError,
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    FileExistsError,
    FileNotFoundError,
    InterruptedError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
    TimeoutError,
)
_ERROR_TYPES: tuple[tuple[type[BaseException], str], ...] = (
    *(
        (cls, f"builtins.{cls.__name__}")
        for cls in (
            *_OS_ERROR_TYPES,
            AttributeError,
            ImportError,
            KeyError,
            MemoryError,
            ModuleNotFoundError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            UnicodeEncodeError,
        )
    ),
    *(
        (cls, f"sqlite3.{cls.__name__}")
        for cls in (
            sqlite3.Error,
            sqlite3.DatabaseError,
            sqlite3.IntegrityError,
            sqlite3.OperationalError,
        )
    ),
    (JSONDecodeError, "json.JSONDecodeError"),
    (BadZipFile, "zipfile.BadZipFile"),
    (CalledProcessError, "subprocess.CalledProcessError"),
    (TimeoutExpired, "subprocess.TimeoutExpired"),
    (gaierror, "socket.gaierror"),
    *(
        (cls, f"keyring.errors.{cls.__name__}")
        for cls in (
            keyring_errors.KeyringError,
            keyring_errors.InitError,
            keyring_errors.KeyringLocked,
            keyring_errors.NoKeyringError,
        )
    ),
    *(
        (cls, f"httpx.{cls.__name__}")
        for cls in (
            httpx.HTTPError,
            httpx.HTTPStatusError,
            httpx.InvalidURL,
            httpx.RequestError,
            httpx.TransportError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
            httpx.LocalProtocolError,
            httpx.ProxyError,
            httpx.TooManyRedirects,
            httpx.UnsupportedProtocol,
            httpx.DecodingError,
        )
    ),
)


def stage_label(stage: str | None) -> str:
    if type(stage) is str:
        return STAGES.get(stage, "Неизвестный этап")
    return "Неизвестный этап"


def _error_details(error: BaseException, status: int | None) -> tuple[str, int | None]:
    from .analysis import OpenRouterError
    from .ffmpeg_download import FFmpegDownloadError
    from .pipeline import NoSpeechError
    from .slides import SlideError
    from .transcription import TranscriptionError
    from .updates import NoPublicRelease, UpdateError

    allowed = (
        *_ERROR_TYPES,
        (OpenRouterError, "ucheba.analysis.OpenRouterError"),
        (FFmpegDownloadError, "ucheba.ffmpeg_download.FFmpegDownloadError"),
        (NoSpeechError, "ucheba.pipeline.NoSpeechError"),
        (SlideError, "ucheba.slides.SlideError"),
        (TranscriptionError, "ucheba.transcription.TranscriptionError"),
        (UpdateError, "ucheba.updates.UpdateError"),
        (NoPublicRelease, "ucheba.updates.NoPublicRelease"),
    )
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        cls = type(current)
        parts = [next((name for known, name in allowed if cls is known), "Неизвестный тип")]
        if cls is gaierror or any(cls is known for known in _OS_ERROR_TYPES):
            for key, minimum, maximum in (("errno", -4095, 4095), ("winerror", 0, 0xFFFFFFFF)):
                value = getattr(current, key, None)
                if type(value) is int and minimum <= value <= maximum:
                    parts.append(f"{key}={value}")
        if status is None and type(current) is httpx.HTTPStatusError:
            response = vars(current).get("response")
            if type(response) is httpx.Response:
                status = response.status_code
        chain.append(" ".join(parts))
        # Даже когда обёртка скрывает traceback, журналу доступен безопасный тип причины.
        descriptors = vars(BaseException)
        cause = descriptors["__cause__"].__get__(current)
        current = cause if cause is not None else descriptors["__context__"].__get__(current)
    return f" · ошибка {' ← '.join(chain)}", status


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
    queue: QueueStore,
    event: str,
    *,
    job: Job | None = None,
    reason: str | None = None,
    stage: str | None = None,
    error: BaseException | None = None,
    status: int | None = None,
    code: str | None = None,
) -> None:
    if type(event) is not str or event not in EVENTS:
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
    if type(reason) is str and reason in REASONS:
        detail += f" · {REASONS[reason]}"
    if type(stage) is str and stage in STAGES:
        detail += f" · этап {STAGES[stage]}"
    if type(code) is str and code in CODES:
        detail += f" · {CODES[code]}"
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    if error is not None:
        error_detail, status = _error_details(error, status)
        detail += error_detail
    if type(status) is int and 100 <= status <= 599:
        detail += f" · HTTP {status}"
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
        app_version = current_version()
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
