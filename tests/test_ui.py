import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


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
