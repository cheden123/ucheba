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
    from PySide6.QtWidgets import QCheckBox, QDialog

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
