import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import delete, isValid

    instance = QApplication.instance()
    qt_app = instance or QApplication([])
    yield qt_app
    qt_app.closeAllWindows()
    for widget in qt_app.topLevelWidgets():
        if isValid(widget):
            delete(widget)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()
    if instance is None:
        delete(qt_app)


def test_window_processes_transcript_and_displays_result(app, tmp_path: Path):
    from PySide6.QtCore import QEventLoop, QSettings, QTimer

    from ucheba.ui import MainWindow

    source = tmp_path / "Лекция.txt"
    source.write_text("Знакомство с подземной гидрогазодинамикой.", encoding="utf-8")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(tmp_path / "queue.sqlite3", settings)
    job = window.queue.add(
        source, tmp_path / "Результаты", model="small", device="cpu", cloud=False
    )
    window.start_next()
    assert window.worker is not None
    loop = QEventLoop()
    window.worker.finished.connect(loop.quit)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(10_000)
    if not window.worker.isFinished():
        loop.exec()
    app.processEvents()
    if window.worker is not None:
        window.worker.stop_requested.set()
        window.worker.wait(10_000)
        pytest.fail("Фоновая обработка расшифровки превысила ожидание теста")
    saved = window.queue.get(job.id)
    assert saved.status == "succeeded"
    assert saved.result and (saved.result / "01_Полный_текст_лекции.docx").is_file()
    window.view.setCurrentIndex(2)
    assert window.table.rowCount() == 1
    window.table.selectRow(0)
    assert window.open_button.isEnabled()
    window.search.setText("нет такой лекции")
    assert window.table.rowCount() == 0
    window.close()


def test_processing_thread_logs_stage_and_safe_cause(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from ucheba import ui
    from ucheba.diagnostics import export_log
    from ucheba.queue import QueueStore
    from ucheba.transcription import TranscriptionError

    database = tmp_path / "queue.sqlite3"
    store = QueueStore(database)
    store.add(tmp_path / "Скрытая лекция.wav", tmp_path, model="small", device="cpu", cloud=False)
    job = store.claim_next()
    assert job is not None

    def fail(_job, _key=None, **kwargs):
        kwargs["stage"]("load_whisper")
        try:
            raise ImportError("C:/private/token-and-content")
        except ImportError as exc:
            raise TranscriptionError("Не удалось загрузить библиотеку Whisper.") from exc

    monkeypatch.setattr(ui, "process", fail)
    parent = QWidget()
    ui.ProcessingThread(database, job, parent).run()
    assert store.get(job.id).status == "failed"
    destination = tmp_path / "journal.txt"
    export_log(store, destination)
    log = destination.read_text(encoding="utf-8-sig")
    assert "Загрузка модели Whisper" in log
    assert "ucheba.transcription.TranscriptionError ← builtins.ImportError" in log
    assert "C:/private" not in log
    assert "Скрытая лекция" not in log


def test_watched_folder_queues_only_stable_file_once(app, tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QSettings

    from ucheba import ui

    watch = tmp_path / "Входящие"
    watch.mkdir()
    source = watch / "Запись.mp4"
    source.write_bytes(b"part")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("output", str(tmp_path / "Результаты"))
    window = ui.MainWindow(tmp_path / "queue.sqlite3", settings)
    window.paused = True
    settings.setValue("watch", str(watch))
    now = 0.0
    monkeypatch.setattr(ui, "monotonic", lambda: now)

    def scan() -> None:
        window.start_scan()
        assert window.scan is not None
        window.scan.join(timeout=5)
        window.consume_scan()

    scan()
    now = 59.0
    source.write_bytes(b"complete")
    scan()
    now = 61.0
    scan()
    assert window.queue.list() == []
    now = 120.0
    scan()
    now = 180.0
    scan()
    jobs = window.queue.list()
    assert len(jobs) == 1
    assert jobs[0].source == source
    assert not jobs[0].cloud
    window.close()


def test_slide_selection_reconfirms_cloud_consent(app, tmp_path: Path):
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog

    from ucheba.ui import MainWindow

    source = tmp_path / "Лекция.mp4"
    source.touch()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("output", str(tmp_path / "Результаты"))
    window = MainWindow(tmp_path / "queue.sqlite3", settings)
    window.paused = True
    observed = {}

    def change_options() -> None:
        dialog = app.activeModalWidget()
        if not isinstance(dialog, QDialog):
            return
        try:
            boxes = dialog.findChildren(QCheckBox)
            cloud = next(box for box in boxes if box.text().startswith("Отправить"))
            slides = next(box for box in boxes if box.text().startswith("Извлечь слайды"))
            preset = next(
                box for box in dialog.findChildren(QComboBox) if box.findData("compact") >= 0
            )
            preset.setCurrentIndex(preset.findData("compact"))
            cloud.setChecked(True)
            slides.setChecked(True)
            observed["checked"] = cloud.isChecked()
            observed["label"] = cloud.text()
        finally:
            dialog.accept()

    QTimer.singleShot(0, change_options)
    window.configure_jobs([source])
    assert observed["checked"] is False
    assert "текст с кадров" in observed["label"]
    (job,) = window.queue.list()
    assert job.slides and not job.cloud
    assert job.auto_speakers
    assert job.template == "compact"
    assert window.document_template() == "compact"
    window.close()


def test_speaker_review_saves_separate_word_file(app, tmp_path: Path):
    from PySide6.QtWidgets import QWidget

    from ucheba.documents import write_json
    from ucheba.queue import Job
    from ucheba.ui import SpeakerReviewDialog

    transcript = tmp_path / "transcript.json"
    write_json(
        transcript,
        [
            {"start": 1, "end": 3, "text": "Тема лекции", "speaker": "Говорящий 1"},
            {"start": 4, "end": 6, "text": "Будет ли контрольная?", "speaker": "Говорящий 2"},
        ],
    )
    original = transcript.read_bytes()
    write_json(tmp_path / "metadata.json", {"lecturer_candidate": "Говорящий 1"})
    job = Job(
        "review",
        tmp_path / "Лекция.mp4",
        tmp_path,
        "small",
        "cpu",
        False,
        "succeeded",
        "",
        tmp_path,
    )
    owner = QWidget()
    dialog = SpeakerReviewDialog(job, tmp_path, owner)
    dialog.loader.join(timeout=5)
    dialog.show_loaded()
    assert "Возможный преподаватель" in dialog.status.text()
    lecturer_name, lecturer_role = dialog.inputs["Говорящий 1"]
    assert lecturer_role.currentData() == "unknown"
    lecturer_name.setText("Иван Петрович")
    lecturer_role.setCurrentIndex(lecturer_role.findData("lecturer"))
    _, student_role = dialog.inputs["Говорящий 2"]
    student_role.setCurrentIndex(student_role.findData("student"))
    dialog.save_choices()
    assert dialog.worker is not None
    assert dialog.worker.wait(10_000)
    app.processEvents()
    assert dialog.worker is None
    assert (tmp_path / "Речь_преподавателей.docx").is_file()
    assert transcript.read_bytes() == original
    dialog.reject()


def test_assignments_navigates_to_slide_with_point_timestamp(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from ucheba import ui
    from ucheba.documents import write_json
    from ucheba.models import Analysis, Assignment
    from ucheba.queue import Job

    task = Assignment(
        title="Лабораторная работа",
        requirements="Сдать отчёт",
        deadline="20 мая",
        quote="Сдать отчёт до 20 мая",
        source_kind="slide",
        start=10,
        end=10,
    )
    write_json(tmp_path / "analysis.json", Analysis(assignments=[task]).model_dump())
    job = Job(
        "tasks", tmp_path / "Лекция.mp4", tmp_path, "small", "cpu", False, "succeeded", "", tmp_path
    )
    owner = QWidget()
    dialog = ui.AssignmentsDialog([job], owner)
    dialog.scan.join(timeout=5)
    dialog.apply_scan()
    assert "Текст слайда" in dialog.details.toPlainText()
    assert task.quote in dialog.details.toPlainText()
    calls = []
    monkeypatch.setattr(
        ui, "show_recording", lambda job, _parent, *, start: calls.append((job, start))
    )
    dialog.open_task()
    assert calls == [(job, 10)]
    dialog.reject()


def test_update_dialog_rechecks_local_installer_after_confirmation(
    app, tmp_path: Path, monkeypatch
):
    from PySide6.QtCore import QSettings

    from ucheba import ui, updates

    source = tmp_path / "Комплект"
    private = tmp_path / "Профиль" / "Updates"
    installer = updates.VerifiedInstaller(
        private / "release" / "UchebaAI-Setup-x64.exe", "9.0.0", "a" * 64, 100, False
    )
    monkeypatch.setattr(ui, "installed_copy", lambda: True)
    monkeypatch.setattr(ui.QFileDialog, "getExistingDirectory", lambda *_args: str(source))
    monkeypatch.setattr(ui.UpdateDialog, "private_update_dir", staticmethod(lambda: private))
    events = []

    def stage(selected, directory, current):
        events.append(("stage", selected, directory, current))
        return installer

    def recheck(verified):
        events.append(("recheck", verified, window.paused))
        raise updates.UpdateError("Контрольная сумма подготовленного установщика изменилась.")

    monkeypatch.setattr(updates, "stage_offline_update", stage)
    monkeypatch.setattr(updates, "verify_installer_for_launch", recheck)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ui.MainWindow(tmp_path / "queue.sqlite3", settings)
    dialog = ui.UpdateDialog(window)

    def confirm(verified):
        events.append(("confirm", verified))
        return True

    monkeypatch.setattr(dialog, "confirm_install", confirm)
    monkeypatch.setattr(dialog, "launch_installer", lambda path: events.append(("launch", path)))

    def settle() -> None:
        worker = dialog.worker
        assert worker is not None and worker.wait(10_000)
        app.processEvents()
        assert dialog.worker is None

    dialog.select_local()
    settle()
    assert dialog.installer == installer
    assert str(installer.path) in dialog.status.text()
    assert "отсутствует" in dialog.status.text()
    dialog.start_install()
    settle()
    assert events == [
        ("stage", source, private, dialog.current_version),
        ("confirm", installer),
        ("recheck", installer, True),
    ]
    assert dialog.installer is None
    assert "Контрольная сумма" in dialog.status.text()
    assert not window.paused
    dialog.reject()
    window.close()


def test_installed_github_update_uses_one_action_before_confirmation(
    app, tmp_path: Path, monkeypatch
):
    from PySide6.QtCore import QSettings

    from ucheba import ui, updates

    release = updates.Update(
        "9.0.0", "Изменения", "https://github.com/cheden123/ucheba/releases/tag/v9.0.0", 100
    )
    prepared = updates.VerifiedInstaller(
        tmp_path / "UchebaAI-Setup-x64.exe", "9.0.0", "a" * 64, 100, False
    )
    monkeypatch.setattr(ui, "installed_copy", lambda: True)
    monkeypatch.setattr(ui.UpdateDialog, "private_update_dir", staticmethod(lambda: tmp_path))
    events = []

    def check(_current, *, require_public):
        events.append(("check", require_public))
        return release

    def stage(info, directory):
        events.append(("stage", info, directory))
        return prepared

    def recheck(installer):
        events.append(("recheck", installer))
        return installer.path

    monkeypatch.setattr(updates, "check_update", check)
    monkeypatch.setattr(updates, "stage_online_update", stage)
    monkeypatch.setattr(updates, "verify_installer_for_launch", recheck)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ui.MainWindow(tmp_path / "queue.sqlite3", settings)
    dialog = ui.UpdateDialog(window)

    def confirm(installer):
        events.append(("confirm", installer))
        return True

    monkeypatch.setattr(dialog, "confirm_install", confirm)
    monkeypatch.setattr(dialog, "launch_installer", lambda path: events.append(("launch", path)))
    dialog.check_button.click()
    for _ in range(3):
        worker = dialog.worker
        assert worker is not None and worker.wait(10_000)
        app.processEvents()
    assert dialog.worker is None
    assert events == [
        ("check", True),
        ("stage", release, tmp_path),
        ("confirm", prepared),
        ("recheck", prepared),
        ("launch", prepared.path),
    ]
    dialog.reject()
    window.close()


def test_key_check_does_not_expose_key_and_exports_safe_log(app, tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QSettings

    from ucheba import ui

    key = "sk-or-v1-mock-private"
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ui.MainWindow(tmp_path / "queue.sqlite3", settings)

    def reject(received):
        assert received == key
        raise ui.OpenRouterError(f"Проверка отклонена: {received}")

    monkeypatch.setattr(ui, "check_openrouter_key", reject)
    check = ui.KeyCheckDialog(key, window.queue, window)
    assert check.worker.wait(10_000)
    app.processEvents()
    assert check.close_button.isEnabled()
    assert "Проверка отклонена: [скрыто]" in check.status.text()
    assert key not in check.worker.key
    check.reject()

    log_name = tmp_path / "journal"
    monkeypatch.setattr(
        ui.QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(log_name), "Текстовые файлы (*.txt)"),
    )
    window.save_diagnostics()
    exported = log_name.with_suffix(".txt").read_text(encoding="utf-8-sig")
    assert "Проверка ключа OpenRouter завершилась ошибкой" in exported
    assert key not in exported
    assert "Журнал обработки сохранён." == window.activity.text()
    window.close()


def test_remove_record_preserves_source_and_results(app, tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QSettings

    from ucheba import ui

    options = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ui.MainWindow(tmp_path / "queue.sqlite3", options)
    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"recording")
    result = tmp_path / "output"
    result.mkdir()
    document = result / "notes.docx"
    document.write_bytes(b"result")
    job = window.queue.add(source, tmp_path, model="base", device="cpu", cloud=False)
    assert window.queue.claim_next() is not None
    window.queue.finish(job.id, "succeeded", result=result)
    window.refresh()
    window.table.selectRow(0)

    monkeypatch.setattr(window, "confirm_delete_job", lambda _job: False)
    window.delete_button.click()
    assert len(window.queue.list()) == 1

    monkeypatch.setattr(window, "confirm_delete_job", lambda _job: True)
    window.delete_button.click()
    assert window.queue.list() == []
    assert window.table.rowCount() == 0
    assert not window.delete_button.isEnabled()
    assert source.read_bytes() == b"recording"
    assert document.read_bytes() == b"result"
    assert source in window.queue.watch_exclusions()
    window.close()


def test_remove_cancelled_job_waits_for_worker_exit(app, tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtCore import QSettings

    from ucheba import ui

    options = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ui.MainWindow(tmp_path / "queue.sqlite3", options)
    job = window.queue.add(
        tmp_path / "lecture.mp4", tmp_path, model="base", device="cpu", cloud=False
    )
    assert window.queue.claim_next() is not None
    window.queue.cancel(job.id)

    window.worker = SimpleNamespace(job=job)
    window.refresh()
    window.table.selectRow(0)
    assert not window.delete_button.isEnabled()
    confirmations = []
    monkeypatch.setattr(
        window, "confirm_delete_job", lambda selected: confirmations.append(selected)
    )
    window.delete_selected()
    assert confirmations == []
    assert len(window.queue.list()) == 1

    window.worker = None
    window.update_actions()
    assert window.delete_button.isEnabled()

    def starts_during_confirmation(_selected):
        window.worker = SimpleNamespace(job=job)
        return True

    monkeypatch.setattr(window, "confirm_delete_job", starts_during_confirmation)
    window.delete_selected()
    assert len(window.queue.list()) == 1
    assert not window.delete_button.isEnabled()
    window.worker = None
    window.close()
