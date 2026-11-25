from pathlib import Path
import sqlite3

import httpx

from ucheba.analysis import OpenRouterError
from ucheba.diagnostics import export_log, record_event, stage_label
from ucheba.queue import QueueStore
from ucheba.transcription import TranscriptionError


def test_diagnostics_only_export_curated_events(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    secret = "sk-or-v1-should-never-appear"
    filename = "Confidential lecture.mp4"
    job = queue.add(
        tmp_path / filename, tmp_path / "Private", model=secret, device="cpu", cloud=False
    )
    record_event(queue, "job_start", job=job)
    record_event(queue, secret)
    record_event(queue, "key_check_ok")
    report = tmp_path / "log.txt"
    export_log(queue, report)
    contents = report.read_text(encoding="utf-8-sig")
    assert "Запущено задание" in contents and "OpenRouter принял ключ" in contents
    assert "cpu" in contents
    assert secret not in contents and filename not in contents and str(tmp_path) not in contents


def test_diagnostics_include_stage_and_curated_causes_without_payload(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    secret = "sk-or-v1-private-key"
    filename = "private-lecture.wav"
    transcript = "Секретный текст лекции"
    error = PermissionError(13, secret, str(tmp_path / filename))
    error.winerror = 5
    wrapped = TranscriptionError(transcript)
    wrapped.__cause__ = error
    error.__cause__ = wrapped
    record_event(queue, "stage_failed", stage="convert", error=wrapped, reason="media")
    record_event(queue, "stage_note", stage="transcribe", code="cuda_fallback")

    # A runtime class can spoof even an approved module and class name.
    unsafe_type = type(secret, (RuntimeError,), {"__module__": transcript})
    spoofed_type = type("PermissionError", (OSError,), {"__module__": "builtins"})
    unsafe_error = unsafe_type(transcript)
    unsafe_error.__cause__ = spoofed_type(22, secret, str(tmp_path / filename))
    record_event(
        queue, "job_failed", stage=secret, reason=filename, error=unsafe_error, code=transcript
    )
    record_event(queue, "stage_note", status=999)

    report = tmp_path / "log.txt"
    export_log(queue, report)
    contents = report.read_text(encoding="utf-8-sig")
    assert "этап Подготовка аудио" in contents
    assert "Медиа или модель Whisper" in contents
    assert contents.count("ucheba.transcription.TranscriptionError") == 1
    assert "builtins.PermissionError errno=13 winerror=5" in contents
    assert "Распознавание продолжено на CPU" in contents
    assert "Неизвестный тип ← Неизвестный тип" in contents
    assert "errno=22" not in contents and "HTTP 999" not in contents
    assert all(value not in contents for value in (secret, filename, transcript, str(tmp_path)))
    assert stage_label("load_whisper") == "Загрузка модели Whisper"
    assert stage_label(secret) == "Неизвестный этап"


def test_diagnostics_include_http_status_and_type_from_wrapped_error(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    secret = "private-token"
    request = httpx.Request("GET", f"https://example.com/{secret}")
    response = httpx.Response(429, request=request, content=secret.encode())

    def check() -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise OpenRouterError(secret) from None

    try:
        check()
    except OpenRouterError as error:
        record_event(queue, "key_check_failed", stage="openrouter_key", error=error)
    record_event(queue, "update_check_failed", stage="update_check", status=503)

    report = tmp_path / "log.txt"
    export_log(queue, report)
    contents = report.read_text(encoding="utf-8-sig")
    assert "ucheba.analysis.OpenRouterError ← httpx.HTTPStatusError · HTTP 429" in contents
    assert "Проверка обновлений · HTTP 503" in contents
    assert secret not in contents and "example.com" not in contents


def test_diagnostics_keep_legacy_rows_and_txt_format(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.db"
    old_stamp = "2026-11-21T12:30:00+05:00"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE diagnostics "
            "(id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO diagnostics(stamp,event,detail) VALUES (?,?,?)",
            (old_stamp, "job_failed", " · задача 0123456789ab · Whisper small · cpu"),
        )

    queue = QueueStore(db_path)
    record_event(queue, "stage_start", stage="save")
    report = tmp_path / "log.txt"
    export_log(queue, report)
    assert report.read_bytes().startswith(b"\xef\xbb\xbf")
    lines = report.read_text(encoding="utf-8-sig").splitlines()
    assert (
        lines[4]
        == f"{old_stamp}  Задание завершилось ошибкой · задача 0123456789ab · Whisper small · cpu"
    )
    assert lines[5].endswith("  Начат этап · этап Сохранение результатов")
