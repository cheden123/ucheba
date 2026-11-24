"""Окно Windows для локальной и облачной обработки лекций."""

from __future__ import annotations

import json
import sys
from bisect import bisect_right
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from pathlib import Path
from stat import S_ISREG
from threading import Event, Thread
from time import monotonic

from keyring.backends.Windows import WinVaultKeyring
from keyring.errors import KeyringError
from PySide6.QtCore import (
    QLockFile,
    QProcess,
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import updates
from .analysis import OpenRouterError
from .documents import timestamp
from .imports import IMPORTS, MEDIA, load_transcript
from .models import Analysis, Assignment, Segment
from .pipeline import partial_result, process
from .queue import Job, QueueStore
from .slides import SlideError
from .speakers import save_review
from .transcription import TranscriptionError
from .updates import Update, UpdateError

KEY_SERVICE = "UchebaAI"
KEY_ACCOUNT = "OpenRouter"
EXTENSIONS = MEDIA | IMPORTS
VIDEO_EXTENSIONS = frozenset(".mp4 .mkv .avi .mov .webm .wmv .m4v .mpeg .ts".split())
STATUS = {
    "queued": "Ожидает",
    "running": "Обрабатывается",
    "succeeded": "Готово",
    "failed": "Ошибка",
    "cancelled": "Отменено",
}
THEMES = {
    "Системная": Qt.ColorScheme.Unknown,
    "Светлая": Qt.ColorScheme.Light,
    "Тёмная": Qt.ColorScheme.Dark,
}
DOCUMENT_TEMPLATES = {
    "academic": "Академический",
    "standard": "Стандартный",
    "compact": "Компактный",
    "modern": "Современный",
}


class ProcessingThread(QThread):
    progress = Signal(str)

    def __init__(self, database: Path, job: Job, parent: QWidget) -> None:
        super().__init__(parent)
        self.database = database
        self.job = job
        self.stop_requested = Event()

    def run(self) -> None:
        queue = QueueStore(self.database)
        job = self.job

        def cancelled() -> bool:
            return self.stop_requested.is_set() or queue.get(job.id).status == "cancelled"

        key = None
        try:
            if job.cloud:
                try:
                    key = WinVaultKeyring().get_password(KEY_SERVICE, KEY_ACCOUNT)
                except (KeyringError, OSError):
                    pass
            result = process(job, key=key, progress=self.progress.emit, cancelled=cancelled)
            queue.finish(job.id, "succeeded", result=result)
        except Exception as exc:
            if self.stop_requested.is_set():
                queue.finish(job.id, "queued", message="Возобновление обработки")
            elif not cancelled():
                # Диагностика провайдера может содержать ключ или фрагменты лекции.
                if type(exc) is ValueError or isinstance(
                    exc, (OpenRouterError, SlideError, TranscriptionError)
                ):
                    message = str(exc)
                elif isinstance(exc, FileNotFoundError):
                    message = "Исходный файл недоступен. Проверьте путь и подключение к папке."
                else:
                    message = "Обработка завершилась ошибкой. Проверьте файл и папку результатов."
                if key:
                    message = message.replace(key, "[скрыто]")
                queue.finish(job.id, "failed", message=message, result=partial_result(job))


class DirectoryScan(Thread):
    def __init__(self, path: Path) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.stop = Event()
        self.files: dict[Path, tuple[int, int]] = {}
        self.available = True

    def run(self) -> None:
        try:
            for path in self.path.iterdir():
                if self.stop.is_set():
                    break
                if path.suffix.lower() not in MEDIA:
                    continue
                try:
                    stat = path.stat()
                    if stat.st_size and S_ISREG(stat.st_mode):
                        self.files[path] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
        except OSError:
            self.available = False


class UpdateThread(QThread):
    def __init__(
        self,
        parent: QWidget,
        action: Callable[[], Update | updates.VerifiedInstaller | Path | None],
    ) -> None:
        super().__init__(parent)
        self.action = action
        self.response: Update | updates.VerifiedInstaller | Path | None = None
        self.error = ""

    def run(self) -> None:
        try:
            self.response = self.action()
        except UpdateError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "Не удалось получить обновление. Проверьте доступ к папке и Интернету."


def installed_copy() -> bool:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    import winreg

    uninstall_key = (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        r"\{9A4F80D8-9AD1-4B56-955B-4F261461413C}_is1"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_key) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
        return (
            isinstance(location, str)
            and Path(location).resolve() == Path(sys.executable).resolve().parent
        )
    except OSError:
        return False


class UpdateDialog(QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.main_window = window
        self.available_update: Update | None = None
        self.installer: updates.VerifiedInstaller | None = None
        self.worker: UpdateThread | None = None
        self.install_confirmed = False
        self.resume_queue_on_failure = False
        self.is_installed = installed_copy()
        try:
            self.current_version: str | None = version("ucheba-ai")
        except PackageNotFoundError:
            self.current_version = None
        self.setWindowTitle("Обновления")
        self.resize(650, 390)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Текущая версия · {self.current_version or 'не определена'}"))
        source_row = QHBoxLayout()
        self.check_button = QPushButton("Проверить GitHub")
        self.check_button.clicked.connect(self.check_online)
        source_row.addWidget(self.check_button)
        self.local_button = QPushButton("Проверить папку с обновлением…")
        self.local_button.clicked.connect(self.select_local)
        source_row.addWidget(self.local_button)
        source_row.addStretch()
        layout.addLayout(source_row)
        self.status = QLabel(
            "Выберите папку с установщиком и SHA256SUMS.txt или проверьте выпуски GitHub."
        )
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName("Что изменилось в новой версии")
        self.notes.setPlaceholderText("Изменения из выпуска GitHub появятся здесь.")
        layout.addWidget(self.notes, 1)
        row = QHBoxLayout()
        self.release_button = QPushButton("Открыть выпуск")
        self.release_button.clicked.connect(self.open_release)
        row.addWidget(self.release_button)
        self.download_button = QPushButton("Загрузить установщик")
        self.download_button.clicked.connect(self.start_download)
        row.addWidget(self.download_button)
        self.folder_button = QPushButton("Открыть папку")
        self.folder_button.clicked.connect(self.open_verified_folder)
        row.addWidget(self.folder_button)
        self.install_button = QPushButton("Установить…")
        self.install_button.clicked.connect(self.start_install)
        row.addWidget(self.install_button)
        self.install_button.setVisible(self.is_installed)
        row.addStretch()
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        if not self.is_installed:
            portable_note = QLabel(
                "Портативную версию обновляйте вручную из нового архива на странице выпуска. "
                "Установленная версия может запускать обновление из этого окна."
            )
            portable_note.setWordWrap(True)
            layout.addWidget(portable_note)
        self.update_buttons()

    def start_request(
        self, action: Callable[[], Update | updates.VerifiedInstaller | Path | None], message: str
    ) -> None:
        worker = UpdateThread(self, action)
        self.worker = worker
        worker.finished.connect(self.request_finished)
        worker.finished.connect(worker.deleteLater)
        self.status.setText(message)
        self.update_buttons()
        worker.start()

    def request_finished(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        result = worker.response
        if self.install_confirmed:
            self.install_confirmed = False
            if isinstance(result, Path) and not worker.error and self.launch_installer(result):
                return
            if worker.error:
                self.installer = None
                self.status.setText(worker.error)
            self.restore_queue()
        elif worker.error:
            self.status.setText(worker.error)
        elif isinstance(result, Update):
            self.available_update = result
            self.notes.setPlainText(result.notes)
            size = f"{result.installer_size / 1024**2:.1f}".replace(".", ",")
            self.status.setText(f"Доступна версия {result.version}. Размер установщика {size} МБ.")
        elif isinstance(result, updates.VerifiedInstaller):
            self.installer = result
            signature = "проверена" if result.signed else "отсутствует"
            self.status.setText(
                f"Проверен установщик версии {result.version}. Подпись издателя · {signature}.\n{result.path}"
            )
        else:
            self.status.setText("Установлена актуальная версия.")
        self.update_buttons()

    def update_buttons(self) -> None:
        idle = self.worker is None
        has_version = self.current_version is not None
        self.check_button.setEnabled(idle and has_version)
        self.local_button.setEnabled(idle and has_version)
        self.close_button.setEnabled(idle)
        self.release_button.setEnabled(self.available_update is not None)
        self.download_button.setEnabled(idle and self.available_update is not None)
        self.folder_button.setEnabled(idle and self.installer is not None)
        self.install_button.setEnabled(idle and self.installer is not None and self.is_installed)

    def check_online(self) -> None:
        if self.worker is None and self.current_version is not None:
            self.available_update = None
            self.notes.clear()
            self.start_request(
                lambda: updates.check_update(self.current_version, require_public=True),
                "Проверка версии на GitHub…",
            )

    @staticmethod
    def private_update_dir() -> Path:
        directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if not directory:
            raise UpdateError("Не удалось определить личную папку обновлений Windows.")
        return Path(directory) / "Updates"

    def select_local(self) -> None:
        if self.worker is not None or self.current_version is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Папка с установщиком и SHA256SUMS.txt")
        if folder:
            self.installer = None
            self.start_request(
                lambda: updates.stage_offline_update(
                    Path(folder), self.private_update_dir(), self.current_version
                ),
                "Проверка локального установщика…",
            )

    def open_release(self) -> None:
        if self.available_update:
            QDesktopServices.openUrl(QUrl(self.available_update.release_url))

    def open_verified_folder(self) -> None:
        if self.installer:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.installer.path.parent)))

    def start_download(self) -> None:
        if (release := self.available_update) and self.worker is None:
            self.installer = None
            self.start_request(
                lambda: updates.stage_online_update(release, self.private_update_dir()),
                "Загрузка и проверка установщика…",
            )

    def start_install(self) -> None:
        if (installer := self.installer) is None or self.worker is not None or not installed_copy():
            return
        if self.main_window.worker:
            self.status.setText("Дождитесь завершения текущей обработки перед установкой.")
            return
        self.resume_queue_on_failure = not self.main_window.paused
        if self.resume_queue_on_failure:
            self.main_window.toggle_pause()
        if self.confirm_install(installer):
            self.install_confirmed = True
            self.start_request(
                lambda: updates.verify_installer_for_launch(installer),
                "Повторная проверка установщика…",
            )
        else:
            self.restore_queue()

    def confirm_install(self, installer: updates.VerifiedInstaller) -> bool:
        detail = "Приложение повторно проверит файл перед запуском."
        if not installer.signed:
            detail += (
                "\n\nSHA-256 проверяет только целостность файла. У этого установщика отсутствует "
                "цифровая подпись. Продолжайте, если получили комплект из доверенного источника. "
                "Возможны предупреждения Windows SmartScreen."
            )
        confirmation = QMessageBox(
            QMessageBox.Icon.Question,
            "Установка обновления",
            f"Закрыть «Учёба AI» и установить обновление {installer.version}?\n\n{detail}",
            parent=self,
        )
        confirm = confirmation.addButton("Установить", QMessageBox.ButtonRole.AcceptRole)
        cancel = confirmation.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirmation.setDefaultButton(cancel)
        confirmation.exec()
        return confirmation.clickedButton() == confirm

    def launch_installer(self, path: Path) -> bool:
        if self.main_window.worker or not installed_copy():
            self.status.setText(
                "Установка доступна после завершения обработки в установленной версии."
            )
            return False
        started, _pid = QProcess.startDetached(str(path), [], str(path.parent))
        if not started:
            self.status.setText("Не удалось запустить установщик.")
            return False
        self.resume_queue_on_failure = False
        self.accept()
        self.main_window.close()
        return True

    def restore_queue(self) -> None:
        if self.resume_queue_on_failure:
            self.resume_queue_on_failure = False
            self.main_window.toggle_pause()

    def reject(self) -> None:
        if self.worker is None:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            event.ignore()
        else:
            super().closeEvent(event)


class LecturePlayer(QDialog):
    def __init__(self, job: Job, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Просмотр лекции")
        self.resize(1050, 650)
        self.segments = self.read_segments(job.result)
        self.starts = [round(segment.start * 1000) for segment in self.segments]
        self.active_line = -1
        self.pending_seek: int | None = None
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.75)
        self.player.setAudioOutput(self.audio)
        self.player.setSource(QUrl.fromLocalFile(str(job.source)))
        layout = QVBoxLayout(self)
        name = QLabel(job.source.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(name)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        video = QVideoWidget()
        splitter.addWidget(video)
        self.player.setVideoOutput(video)

        tabs = QTabWidget()
        splitter.addWidget(tabs)
        splitter.setSizes([600, 400])
        transcript_panel = QWidget()
        transcript_layout = QVBoxLayout(transcript_panel)
        search = QLineEdit()
        search.setPlaceholderText("Поиск по расшифровке")
        search.setAccessibleName("Поиск по расшифровке")
        transcript_layout.addWidget(search)
        self.transcript = QListWidget()
        for segment in self.segments:
            text = f"[{timestamp(segment.start)}] {segment.text}"
            self.add_cue(self.transcript, text, segment.start)
        if not self.segments:
            transcript_layout.addWidget(QLabel("Расшифровка с временными метками недоступна."))
        transcript_layout.addWidget(self.transcript, 1)
        self.transcript.itemActivated.connect(self.seek_cue)
        self.transcript.itemClicked.connect(self.seek_cue)
        search.textChanged.connect(self.filter_transcript)
        tabs.addTab(transcript_panel, "Расшифровка")
        tasks = QListWidget()
        for item in self.read_analysis(job.result).assignments:
            self.add_cue(tasks, f"[{timestamp(item.start)}] {item.title}", item.start)
        tasks.itemActivated.connect(self.seek_cue)
        tasks.itemClicked.connect(self.seek_cue)
        if tasks.count():
            tabs.addTab(tasks, "Задания")

        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Позиция воспроизведения")
        self.seek.setRange(0, 0)
        self.seek.valueChanged.connect(self.player.setPosition)
        layout.addWidget(self.seek)
        controls = QHBoxLayout()
        self.play = QPushButton("Воспроизвести")
        self.play.clicked.connect(self.toggle_play)
        controls.addWidget(self.play)
        self.time = QLabel("00:00:00 / 00:00:00")
        controls.addWidget(self.time)
        controls.addStretch()
        speed = QComboBox()
        speed.setAccessibleName("Скорость воспроизведения")
        for rate in (0.75, 1.0, 1.25, 1.5, 2.0):
            speed.addItem(f"{rate:g}×".replace(".", ","), rate)
        speed.setCurrentIndex(1)
        speed.currentIndexChanged.connect(
            lambda _index: self.player.setPlaybackRate(speed.currentData())
        )
        controls.addWidget(speed)
        controls.addWidget(QLabel("Громкость"))
        volume = QSlider(Qt.Orientation.Horizontal)
        volume.setAccessibleName("Громкость")
        volume.setRange(0, 100)
        volume.setValue(75)
        volume.setFixedWidth(120)
        volume.valueChanged.connect(lambda level: self.audio.setVolume(level / 100))
        controls.addWidget(volume)
        layout.addLayout(controls)
        self.error = QLabel("Не удалось воспроизвести запись. Проверьте файл и доступ к нему.")
        self.error.hide()
        layout.addWidget(self.error)
        self.player.playbackStateChanged.connect(self.update_button)
        self.player.durationChanged.connect(self.update_duration)
        self.player.positionChanged.connect(self.update_position)
        self.player.seekableChanged.connect(self.apply_pending_seek)
        self.player.errorOccurred.connect(lambda *_args: self.error.show())
        self.finished.connect(lambda _result: self.player.stop())
        if self.player.error() != QMediaPlayer.Error.NoError:
            self.error.show()

    @staticmethod
    def read_segments(result: Path | None) -> list[Segment]:
        if result:
            try:
                return [
                    seg
                    for seg in load_transcript(result / "transcript.json")
                    if seg.end > seg.start
                ]
            except (OSError, ValueError):
                pass
        return []

    @staticmethod
    def read_analysis(result: Path | None) -> Analysis:
        if result:
            try:
                limit = 16 * 1024 * 1024
                with (result / "analysis.json").open("rb") as stream:
                    data = stream.read(limit + 1)
                if len(data) <= limit:
                    analysis = Analysis.model_validate_json(data)
                    analysis.assignments = [
                        task
                        for task in analysis.assignments
                        if isfinite(task.start)
                        and isfinite(task.end)
                        and 0 <= task.start <= task.end
                    ]
                    return analysis
            except (OSError, ValueError):
                pass
        return Analysis()

    @staticmethod
    def add_cue(target: QListWidget, text: str, start: float) -> None:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, round(start * 1000))
        item.setToolTip(text)
        target.addItem(item)

    def seek_cue(self, item: QListWidgetItem) -> None:
        self.seek_to(item.data(Qt.ItemDataRole.UserRole))

    def seek_to(self, position: int) -> None:
        if self.player.isSeekable():
            self.player.setPosition(position)
        else:
            self.pending_seek = position

    def apply_pending_seek(self, seekable: bool) -> None:
        if seekable and self.pending_seek is not None:
            self.player.setPosition(self.pending_seek)
            self.pending_seek = None

    def filter_transcript(self, text: str) -> None:
        query = text.strip().casefold()
        for index in range(self.transcript.count()):
            item = self.transcript.item(index)
            item.setHidden(query not in item.text().casefold())

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def update_button(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play.setText(
            "Пауза" if state == QMediaPlayer.PlaybackState.PlayingState else "Воспроизвести"
        )

    def update_duration(self, duration: int) -> None:
        self.seek.setMaximum(duration)
        self.update_position(self.player.position())

    def update_position(self, position: int) -> None:
        self.seek.blockSignals(True)
        self.seek.setValue(position)
        self.seek.blockSignals(False)
        self.time.setText(
            f"{timestamp(position / 1000)} / {timestamp(self.player.duration() / 1000)}"
        )
        line = bisect_right(self.starts, position) - 1
        if line >= 0 and position >= round(self.segments[line].end * 1000):
            line = -1
        if line != self.active_line:
            self.active_line = line
            self.transcript.setCurrentRow(line)


def show_recording(
    job: Job, parent: QWidget, *, start: float = 0, end: float | None = None
) -> None:
    player = LecturePlayer(job, parent)
    if start:
        player.seek_to(round(start * 1000))
    if end is not None:
        stop_at = round(end * 1000)

        def stop_preview(position: int) -> None:
            if position >= stop_at:
                player.player.positionChanged.disconnect(stop_preview)
                player.player.pause()

        player.player.positionChanged.connect(stop_preview)
        player.player.play()
    player.exec()
    player.deleteLater()


class AssignmentScan(Thread):
    def __init__(self, jobs: list[Job]) -> None:
        super().__init__(daemon=True)
        self.jobs = jobs
        self.entries: list[tuple[Job, Assignment]] = []
        self.stop = Event()

    def run(self) -> None:
        for job in self.jobs:
            if self.stop.is_set():
                break
            analysis = LecturePlayer.read_analysis(job.result)
            self.entries.extend((job, task) for task in analysis.assignments)


class AssignmentsDialog(QDialog):
    def __init__(self, jobs: list[Job], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Учебные задания")
        self.resize(900, 560)
        self.entries: list[tuple[Job, Assignment]] = []
        self.displayed: list[tuple[Job, Assignment]] = []
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по заданию, сроку или лекции")
        self.search.setAccessibleName("Поиск по заданиям")
        self.search.textChanged.connect(self.refresh)
        layout.addWidget(self.search)
        panes = QSplitter(Qt.Orientation.Horizontal)
        self.rows = QListWidget()
        self.rows.setAccessibleName("Найденные задания")
        self.rows.currentRowChanged.connect(self.select_task)
        self.rows.itemActivated.connect(lambda _item: self.open_task())
        panes.addWidget(self.rows)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Подробности выбранного задания")
        panes.addWidget(self.details)
        panes.setSizes([350, 550])
        layout.addWidget(panes, 1)
        row = QHBoxLayout()
        self.open_button = QPushButton("Перейти к фрагменту записи")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_task)
        row.addWidget(self.open_button)
        row.addStretch()
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.details.setPlaceholderText("Загрузка заданий из библиотеки…")
        self.scan = AssignmentScan([job for job in jobs if job.result])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.apply_scan)
        self.timer.start(150)
        self.finished.connect(self.stop_scan)
        self.scan.start()

    def stop_scan(self, _result: int) -> None:
        self.timer.stop()
        self.scan.stop.set()

    def apply_scan(self) -> None:
        if self.scan.is_alive():
            return
        self.timer.stop()
        self.entries = self.scan.entries
        self.refresh()

    def refresh(self) -> None:
        query = self.search.text().strip().casefold()
        self.displayed = [
            (job, task)
            for job, task in self.entries
            if query
            in f"{task.title} {task.requirements} {task.deadline or ''} {job.source.name}".casefold()
        ]
        self.rows.clear()
        for job, task in self.displayed:
            self.rows.addItem(f"{task.title}\n{job.source.name}")
        if self.displayed:
            self.rows.setCurrentRow(0)
        elif not self.scan.is_alive():
            self.details.setPlaceholderText("Задания не найдены.")

    def current_task(self) -> tuple[Job, Assignment] | None:
        row = self.rows.currentRow()
        return self.displayed[row] if 0 <= row < len(self.displayed) else None

    def select_task(self, _row: int) -> None:
        selection = self.current_task()
        self.open_button.setEnabled(False)
        if selection is None:
            self.details.clear()
            return
        job, task = selection
        timed = (
            task.end > task.start or task.source_kind == "slide"
        ) and job.source.suffix.lower() in MEDIA
        self.open_button.setEnabled(timed)
        lines = [
            task.title,
            f"Лекция · {job.source.name}",
            f"Срок сдачи · {task.deadline or 'Не указан'}",
            f"Источник · {'Текст слайда' if task.source_kind == 'slide' else 'Расшифровка речи'}",
        ]
        if task.speaker:
            lines.append(f"Говорящий · {task.speaker}")
        if timed:
            lines.append(f"Фрагмент · {timestamp(task.start)}")
        lines.extend(["", task.requirements, "", f"«{task.quote}»"])
        if task.plan:
            lines.extend(
                ["", "План выполнения", *[f"{n}. {step}" for n, step in enumerate(task.plan, 1)]]
            )
        self.details.setPlainText("\n".join(lines))

    def open_task(self) -> None:
        if self.open_button.isEnabled() and (selection := self.current_task()):
            job, task = selection
            show_recording(job, self, start=task.start)


SPEAKER_ROLES = {
    "unknown": "Не назначена",
    "lecturer": "Преподаватель",
    "student": "Студент",
    "ignore": "Исключить",
}


def read_small_object(path: Path) -> dict[str, object]:
    limit = 1024 * 1024
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        value = json.loads(raw) if len(raw) <= limit else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class SpeakerLoad(Thread):
    def __init__(self, result: Path) -> None:
        super().__init__(daemon=True)
        self.result = result
        self.previews: dict[str, Segment] = {}
        self.review: dict[str, object] = {}
        self.candidate: str | None = None
        self.error = ""

    def run(self) -> None:
        try:
            segments = load_transcript(self.result / "transcript.json")
        except (OSError, ValueError):
            self.error = "Не удалось прочитать исходную расшифровку."
            return
        for segment in segments:
            if not segment.speaker:
                continue
            previous = self.previews.get(segment.speaker)
            if previous is None or segment.end - segment.start > previous.end - previous.start:
                self.previews[segment.speaker] = segment
        metadata = read_small_object(self.result / "metadata.json")
        candidate = metadata.get("lecturer_candidate")
        if isinstance(candidate, str) and candidate in self.previews:
            self.candidate = candidate
        self.review = read_small_object(self.result / "speaker_review.json")


class SpeakerSave(QThread):
    def __init__(self, parent: QWidget, result: Path, choices: dict[str, tuple[str, str]]) -> None:
        super().__init__(parent)
        self.result_path = result
        self.choices = choices
        self.error = ""

    def run(self) -> None:
        try:
            save_review(self.result_path, self.choices)
        except Exception as exc:
            self.error = (
                str(exc)
                if type(exc) is ValueError
                else "Не удалось сохранить документ. Проверьте доступ к папке результатов."
            )


class SpeakerReviewDialog(QDialog):
    def __init__(self, job: Job, result: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.job = job
        self.result_path = result
        self.inputs: dict[str, tuple[QLineEdit, QComboBox]] = {}
        self.worker: SpeakerSave | None = None
        self.setWindowTitle("Говорящие в лекции")
        self.resize(800, 450)
        layout = QVBoxLayout(self)
        self.status = QLabel("Чтение расшифровки…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Говорящий", "Имя", "Роль", "Фрагмент"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        self.save_button = QPushButton("Сохранить документ")
        self.save_button.clicked.connect(self.save_choices)
        self.save_button.setEnabled(False)
        footer.addWidget(self.save_button)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.loader = SpeakerLoad(result)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.show_loaded)
        self.timer.start(150)
        self.finished.connect(lambda _result: self.timer.stop())
        self.loader.start()

    def show_loaded(self) -> None:
        if self.loader.is_alive():
            return
        self.timer.stop()
        if self.loader.error:
            self.status.setText(self.loader.error)
            return
        if not self.loader.previews:
            self.status.setText(
                "В расшифровке нет меток говорящих. Для новой обработки выберите локальную "
                "модель Community-1 или импортируйте расшифровку JSON с полем speaker."
            )
            return
        message = (
            "Назначьте роли, чтобы сохранить отдельный документ с речью преподавателей и студентов."
        )
        if self.loader.candidate:
            message += f" Возможный преподаватель — {self.loader.candidate}. Подтвердите роль после прослушивания."
        self.status.setText(message)
        self.table.setRowCount(len(self.loader.previews))
        for row, (speaker, sample) in enumerate(self.loader.previews.items()):
            self.add_speaker(row, speaker, sample)
        self.save_button.setEnabled(True)

    def add_speaker(self, row: int, speaker: str, sample: Segment) -> None:
        self.table.setItem(row, 0, QTableWidgetItem(speaker))
        saved = self.loader.review.get(speaker)
        saved_name = saved.get("name") if isinstance(saved, dict) else None
        saved_role = saved.get("role") if isinstance(saved, dict) else None
        name = QLineEdit(saved_name if isinstance(saved_name, str) else speaker)
        name.setMaxLength(160)
        name.setAccessibleName(f"Имя · {speaker}")
        self.table.setCellWidget(row, 1, name)
        role = QComboBox()
        role.setAccessibleName(f"Роль · {speaker}")
        for code, label in SPEAKER_ROLES.items():
            role.addItem(label, code)
        if isinstance(saved_role, str) and saved_role in SPEAKER_ROLES:
            role.setCurrentIndex(role.findData(saved_role))
        self.table.setCellWidget(row, 2, role)
        preview = QPushButton("Прослушать")
        preview.setEnabled(sample.end > sample.start and self.job.source.suffix.lower() in MEDIA)
        preview.clicked.connect(lambda _checked: self.preview(sample))
        self.table.setCellWidget(row, 3, preview)
        self.inputs[speaker] = (name, role)

    def preview(self, sample: Segment) -> None:
        show_recording(self.job, self, start=sample.start, end=min(sample.end, sample.start + 20))

    def save_choices(self) -> None:
        choices: dict[str, tuple[str, str]] = {
            speaker: (name.text().strip(), role.currentData())
            for speaker, (name, role) in self.inputs.items()
        }
        if not all(name for name, _role in choices.values()):
            self.status.setText("Укажите имя для каждого говорящего.")
            return
        if not any(role == "lecturer" for _name, role in choices.values()):
            self.status.setText("Подтвердите хотя бы одного преподавателя.")
            return
        worker = SpeakerSave(self, self.result_path, choices)
        self.worker = worker
        worker.finished.connect(self.on_saved)
        worker.finished.connect(worker.deleteLater)
        self.status.setText("Сохранение документа…")
        self.table.setEnabled(False)
        self.save_button.setEnabled(False)
        self.close_button.setEnabled(False)
        worker.start()

    def on_saved(self) -> None:
        worker = self.worker
        self.worker = None
        self.table.setEnabled(True)
        self.save_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.status.setText(
            worker.error
            if worker and worker.error
            else "Документ сохранён в папке результатов лекции."
        )

    def reject(self) -> None:
        if self.worker is None:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            event.ignore()
        else:
            super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, database: Path, settings: QSettings | None = None) -> None:
        super().__init__()
        self.database = database
        self.queue = QueueStore(database)
        self.settings = settings if settings is not None else QSettings()
        self.apply_theme(str(self.settings.value("theme", "Системная")))
        self.worker: ProcessingThread | None = None
        self.scan: DirectoryScan | None = None
        self.observed: dict[Path, tuple[tuple[int, int], float]] = {}
        self.closing = False
        self.paused = False
        self.visible_jobs: list[Job] = []
        self.setWindowTitle("Учёба AI")
        self.resize(1080, 660)
        self.setMinimumSize(680, 460)
        self.setAcceptDrops(True)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)
        self.setCentralWidget(root)

        title = QLabel("Лекции")
        title.setStyleSheet("font-size: 28px; font-weight: 600")
        layout.addWidget(title)

        actions = QHBoxLayout()
        self.button(actions, "Добавить файлы…", self.choose_files, primary=True)
        self.button(actions, "Добавить папку…", self.choose_folder)
        actions.addStretch()
        self.button(actions, "Задания…", self.show_assignments)
        self.button(actions, "Обновления…", self.show_updates)
        self.button(actions, "Настройки…", self.edit_settings)
        layout.addLayout(actions)

        filters = QHBoxLayout()
        self.view = QComboBox()
        self.view.addItems(["Все лекции", "Очередь", "Библиотека"])
        self.view.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.view)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени файла")
        self.search.setAccessibleName("Поиск по имени файла")
        self.search.textChanged.connect(self.refresh)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Лекция", "Состояние", "Модель Whisper", "Сообщение"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.itemDoubleClicked.connect(lambda _: self.open_result())
        layout.addWidget(self.table, 1)

        job_actions = QHBoxLayout()
        self.open_button = self.button(job_actions, "Открыть результаты", self.open_result)
        self.player_button = self.button(job_actions, "Просмотр записи", self.open_player)
        self.speakers_button = self.button(job_actions, "Говорящие…", self.review_speakers)
        self.retry_button = self.button(job_actions, "Повторить", self.retry_selected)
        self.cancel_button = self.button(job_actions, "Отменить", self.cancel_selected)
        job_actions.addStretch()
        self.pause_button = self.button(job_actions, "Приостановить очередь", self.toggle_pause)
        layout.addLayout(job_actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Обработка лекции")
        self.progress.hide()
        self.activity = QLabel("Добавьте файлы или перетащите их в это окно.")
        self.activity.setTextFormat(Qt.TextFormat.PlainText)
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        self.watch_status = QLabel()
        self.watch_status.hide()
        layout.addWidget(self.watch_status)
        layout.addWidget(self.progress)
        self.refresh()
        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(250)
        self.scan_timer.timeout.connect(self.consume_scan)
        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(30_000)
        self.watch_timer.timeout.connect(self.start_scan)
        self.watch_timer.start()
        self.start_scan()
        QTimer.singleShot(0, self.start_next)

    @staticmethod
    def button(layout, label, handler, *, primary=False) -> QPushButton:
        button = QPushButton(label)
        button.setMinimumHeight(34)
        if primary:
            button.setStyleSheet(
                "QPushButton { background: #365bc9; color: white; border: 0; "
                "border-radius: 6px; padding: 0 14px; font-weight: 600; } "
                "QPushButton:hover, QPushButton:focus { background: #2847aa; }"
            )
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    def selected_job(self) -> Job | None:
        row = self.table.currentRow()
        return self.visible_jobs[row] if 0 <= row < len(self.visible_jobs) else None

    def refresh(self) -> None:
        selected = self.selected_job()
        query = self.search.text().casefold().strip()
        view = self.view.currentIndex()
        self.visible_jobs = [
            job
            for job in self.queue.list()
            if query in job.source.name.casefold()
            and (view != 1 or job.status != "succeeded")
            and (view != 2 or job.result is not None)
        ]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.visible_jobs))
        for row, job in enumerate(self.visible_jobs):
            for column, value in enumerate(
                [job.source.name, STATUS[job.status], job.model, job.message]
            ):
                item = QTableWidgetItem(value)
                item.setToolTip(str(job.source) if column == 0 else value)
                self.table.setItem(row, column, item)
            if selected and selected.id == job.id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.update_actions()

    def update_actions(self) -> None:
        job = self.selected_job()
        self.open_button.setEnabled(bool(job and job.result))
        self.player_button.setEnabled(bool(job and job.source.suffix.lower() in MEDIA))
        self.speakers_button.setEnabled(bool(job and job.result and job.status == "succeeded"))
        self.retry_button.setEnabled(
            bool(
                job
                and job.status in {"failed", "cancelled"}
                and (not self.worker or self.worker.job.id != job.id)
            )
        )
        self.cancel_button.setEnabled(bool(job and job.status in {"queued", "running"}))

    def choose_files(self) -> None:
        formats = " ".join(f"*{extension}" for extension in sorted(EXTENSIONS))
        names, _ = QFileDialog.getOpenFileNames(
            self, "Выберите записи лекций", "", f"Лекции и расшифровки ({formats})"
        )
        self.configure_jobs([Path(name) for name in names])

    def choose_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Выберите папку с лекциями")
        if not name:
            return
        try:
            paths = sorted(path for path in Path(name).iterdir() if path.is_file())
        except OSError:
            QMessageBox.warning(self, "Папка недоступна", "Проверьте доступ к выбранной папке.")
            return
        self.configure_jobs(paths or [Path(name)])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        sources = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        self.configure_jobs(sources)

    def configure_jobs(self, paths: list[Path]) -> None:
        if not paths:
            return
        sources = list(
            dict.fromkeys(
                path for path in paths if path.suffix.lower() in EXTENSIONS and path.is_file()
            )
        )
        if not sources:
            QMessageBox.information(self, "Добавление лекций", "Поддерживаемые файлы не найдены.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Новая лекция")
        form = QFormLayout(dialog)
        selection = QLabel(
            sources[0].name if len(sources) == 1 else f"Выбрано файлов: {len(sources)}"
        )
        selection.setTextFormat(Qt.TextFormat.PlainText)
        selection.setWordWrap(True)
        form.addRow(selection)
        output = QLineEdit(str(self.settings.value("output", self.default_output())))
        output_row = QHBoxLayout()
        output_row.addWidget(output, 1)

        def select_output() -> None:
            name = QFileDialog.getExistingDirectory(dialog, "Папка для документов", output.text())
            if name:
                output.setText(name)

        self.button(output_row, "Выбрать…", select_output)
        form.addRow("Папка для документов", output_row)
        models = QComboBox()
        models.addItems(["small", "base", "medium", "large-v3-turbo", "large-v3"])
        models.setCurrentText(str(self.settings.value("model", "small")))
        form.addRow("Модель Whisper", models)
        device = QComboBox()
        device.addItem("Процессор", "cpu")
        device.addItem("Видеокарта NVIDIA", "cuda")
        device.addItem("Автоматически", "auto")
        device.setCurrentIndex(max(device.findData(self.settings.value("device", "cpu")), 0))
        form.addRow("Устройство", device)
        template = QComboBox()
        for code, label in DOCUMENT_TEMPLATES.items():
            template.addItem(label, code)
        template.setCurrentIndex(max(template.findData(self.document_template()), 0))
        form.addRow("Оформление Word", template)
        speaker_model = str(self.settings.value("speaker_model", "")).strip()
        use_speakers = QCheckBox("Разделить говорящих локальной моделью Community-1")
        media_selected = any(source.suffix.lower() in MEDIA for source in sources)
        engine_available = not getattr(sys, "frozen", False)
        use_speakers.setEnabled(engine_available and bool(speaker_model) and media_selected)
        if not engine_available:
            use_speakers.setToolTip("Автоматическая диаризация доступна при запуске из исходников.")
        elif not speaker_model:
            use_speakers.setToolTip("Укажите папку модели Community-1 в настройках.")
        form.addRow(use_speakers)
        slides = QCheckBox("Извлечь слайды и распознать текст на кадрах")
        slides.setEnabled(any(source.suffix.lower() in VIDEO_EXTENSIONS for source in sources))
        form.addRow(slides)
        cloud = QCheckBox("Отправить текст расшифровки в OpenRouter для анализа")
        form.addRow(cloud)
        slide_privacy = QLabel(
            "При облачном анализе отправляется и текст, распознанный на кадрах. "
            "В нём могут оказаться видимые сообщения чата и письма. Сами изображения не отправляются."
        )
        slide_privacy.setWordWrap(True)
        slide_privacy.hide()
        form.addRow(slide_privacy)

        def change_slide_consent(enabled: bool) -> None:
            cloud.setChecked(False)
            cloud.setText(
                "Отправить расшифровку и текст с кадров в OpenRouter для анализа"
                if enabled
                else "Отправить текст расшифровки в OpenRouter для анализа"
            )
            slide_privacy.setVisible(enabled)

        slides.toggled.connect(change_slide_consent)
        free_note = QLabel(
            "Для бесплатного анализа используйте рабочую область OpenRouter без собственных ключей провайдеров."
        )
        free_note.setWordWrap(True)
        form.addRow(free_note)
        local = QLabel("Без облачного анализа приложение создаст полную расшифровку.")
        local.setWordWrap(True)
        form.addRow(local)
        if media_selected:
            model_note = QLabel("При первом распознавании Whisper загрузит выбранную модель.")
            model_note.setWordWrap(True)
            form.addRow(model_note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Обработать лекции")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = Path(output.text().strip())
        if not target.is_absolute():
            QMessageBox.warning(self, "Папка для документов", "Укажите полный путь к папке.")
            return
        self.settings.setValue("output", str(target))
        self.settings.setValue("model", models.currentText())
        self.settings.setValue("device", device.currentData())
        self.settings.setValue("template", template.currentData())
        unavailable = []
        for source in sources:
            try:
                self.queue.add(
                    source,
                    target,
                    model=models.currentText(),
                    device=device.currentData(),
                    cloud=cloud.isChecked(),
                    template=template.currentData(),
                    slides=slides.isChecked() and source.suffix.lower() in VIDEO_EXTENSIONS,
                    speaker_model=(
                        Path(speaker_model)
                        if use_speakers.isChecked() and source.suffix.lower() in MEDIA
                        else None
                    ),
                )
            except OSError:
                unavailable.append(source.name)
        if unavailable:
            warning = QMessageBox(
                QMessageBox.Icon.Warning,
                "Файлы недоступны",
                "Не удалось добавить некоторые файлы. Проверьте доступ к ним.",
                parent=self,
            )
            warning.setDetailedText("\n".join(unavailable))
            warning.exec()
        self.refresh()
        self.start_next()

    @staticmethod
    def default_output() -> str:
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        return str(Path(documents) / "Учёба AI")

    def document_template(self) -> str:
        preset = str(self.settings.value("template", "academic"))
        return preset if preset in DOCUMENT_TEMPLATES else "academic"

    def edit_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки")
        form = QFormLayout(dialog)
        secret = QLineEdit()
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        secret.setPlaceholderText("Введите новый ключ")
        delete = QCheckBox("Удалить сохранённый ключ")
        try:
            stored = bool(WinVaultKeyring().get_password(KEY_SERVICE, KEY_ACCOUNT))
        except (KeyringError, OSError):
            stored = False
        delete.setEnabled(stored)
        form.addRow(QLabel("Ключ хранится в диспетчере учётных данных Windows."))
        form.addRow("Ключ OpenRouter", secret)
        form.addRow(delete)
        theme = QComboBox()
        theme.addItems(list(THEMES))
        theme.setCurrentText(str(self.settings.value("theme", "Системная")))
        form.addRow("Цветовая тема", theme)
        watch = QLineEdit(str(self.settings.value("watch", "")))
        watch.setPlaceholderText(r"Например, \\Synology\Лекции")
        form.addRow("Наблюдаемая папка", watch)
        watch_note = QLabel(
            "Укажите полный путь для автоматического распознавания. "
            "Видео и аудио обрабатываются локально."
        )
        watch_note.setWordWrap(True)
        form.addRow(watch_note)
        speaker_path = QLineEdit(str(self.settings.value("speaker_model", "")))
        speaker_path.setPlaceholderText("Папка локально загруженной модели")
        model_row = QHBoxLayout()
        model_row.addWidget(speaker_path, 1)

        def choose_speaker_model() -> None:
            directory = QFileDialog.getExistingDirectory(
                dialog, "Выберите папку модели Community-1", speaker_path.text()
            )
            if directory:
                speaker_path.setText(directory)

        self.button(model_row, "Выбрать…", choose_speaker_model)
        form.addRow("Модель говорящих Community-1", model_row)

        def show_hardware() -> None:
            from .hardware import hardware_lines

            destination = Path(str(self.settings.value("output", self.default_output())))
            QMessageBox.information(dialog, "Оборудование", "\n".join(hardware_lines(destination)))

        info_row = QHBoxLayout()
        self.button(info_row, "Показать сведения об оборудовании", show_hardware)
        form.addRow(info_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            watch_path = watch.text().strip()
            model_path = speaker_path.text().strip()
            if model_path and not Path(model_path).is_absolute():
                QMessageBox.warning(self, "Модель говорящих", "Укажите полный путь к папке модели.")
                return
            if watch_path and not Path(watch_path).is_absolute():
                QMessageBox.warning(self, "Наблюдаемая папка", "Укажите полный путь к папке.")
                return
            if watch_path != str(self.settings.value("watch", "")):
                self.observed.clear()
                self.settings.setValue("watch", watch_path)
                self.watch_status.hide()
                if self.scan:
                    self.scan.stop.set()
                else:
                    self.start_scan()
            self.settings.setValue("speaker_model", model_path)
            self.settings.setValue("theme", theme.currentText())
            self.apply_theme(theme.currentText())
            try:
                if delete.isChecked():
                    WinVaultKeyring().delete_password(KEY_SERVICE, KEY_ACCOUNT)
                elif new_key := secret.text().strip():
                    WinVaultKeyring().set_password(KEY_SERVICE, KEY_ACCOUNT, new_key)
            except (KeyringError, OSError):
                QMessageBox.warning(
                    self, "Ошибка сохранения", "Хранилище учётных данных недоступно."
                )

    @staticmethod
    def apply_theme(theme: str) -> None:
        QApplication.styleHints().setColorScheme(THEMES.get(theme, Qt.ColorScheme.Unknown))

    def start_scan(self) -> None:
        watch = str(self.settings.value("watch", ""))
        if not watch or self.scan is not None or self.closing:
            return
        scan = DirectoryScan(Path(watch))
        self.scan = scan
        scan.start()
        self.scan_timer.start()

    def consume_scan(self) -> None:
        scan = self.scan
        if scan is None or scan.is_alive():
            return
        self.scan_timer.stop()
        self.scan = None
        if self.closing:
            return
        watch = str(self.settings.value("watch", ""))
        if not watch or scan.path != Path(watch) or scan.stop.is_set():
            self.start_scan()
            return
        if not scan.available:
            self.observed.clear()
            self.watch_status.setText(
                "Наблюдаемая папка недоступна. Проверка повторится автоматически."
            )
            self.watch_status.show()
            return
        self.watch_status.hide()
        existing = {job.source for job in self.queue.list()}
        now = monotonic()
        seen: dict[Path, tuple[tuple[int, int], float]] = {}
        for path, signature in scan.files.items():
            if path in existing:
                continue
            last_signature, since = self.observed.get(path, (signature, now))
            if signature != last_signature:
                since = now
            if now - since >= 60:
                self.queue.add(
                    path,
                    Path(str(self.settings.value("output", self.default_output()))),
                    model=str(self.settings.value("model", "small")),
                    device=str(self.settings.value("device", "cpu")),
                    cloud=False,
                    template=self.document_template(),
                )
            else:
                seen[path] = (signature, since)
        self.observed = seen
        self.refresh()
        self.start_next()

    def start_next(self) -> None:
        if self.worker or self.paused or self.closing:
            return
        job = self.queue.claim_next()
        if job is None:
            return
        self.worker = ProcessingThread(self.database, job, self)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.finished.connect(self.worker.deleteLater)
        self.progress.show()
        self.activity.setText(f"{job.source.name} · Подготовка")
        self.refresh()
        self.worker.start()

    def show_progress(self, stage: str) -> None:
        if self.worker and not self.closing:
            self.activity.setText(f"{self.worker.job.source.name} · {stage}")

    def on_finished(self) -> None:
        self.worker = None
        self.progress.hide()
        self.activity.setText("Очередь приостановлена." if self.paused else "Готово к обработке.")
        self.refresh()
        if self.closing:
            self.close()
        else:
            self.start_next()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.setText("Продолжить очередь" if self.paused else "Приостановить очередь")
        if not self.worker:
            self.activity.setText(
                "Очередь приостановлена." if self.paused else "Готово к обработке."
            )
        self.start_next()

    def retry_selected(self) -> None:
        if job := self.selected_job():
            self.queue.retry(job.id)
            self.refresh()
            self.start_next()

    def cancel_selected(self) -> None:
        if job := self.selected_job():
            self.queue.cancel(job.id)
            self.refresh()

    def open_result(self) -> None:
        job = self.selected_job()
        if job and job.result and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.result))):
            QMessageBox.warning(self, "Папка недоступна", "Не удалось открыть папку результатов.")

    def open_player(self) -> None:
        if (job := self.selected_job()) and job.source.suffix.lower() in MEDIA:
            show_recording(job, self)

    def show_assignments(self) -> None:
        dialog = AssignmentsDialog(self.queue.list(), self)
        dialog.exec()
        dialog.deleteLater()

    def review_speakers(self) -> None:
        if (job := self.selected_job()) and job.result and job.status == "succeeded":
            dialog = SpeakerReviewDialog(job, job.result, self)
            dialog.exec()
            dialog.deleteLater()

    def show_updates(self) -> None:
        dialog = UpdateDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.scan:
            self.scan.stop.set()
        if self.worker:
            self.closing = True
            self.worker.stop_requested.set()
            self.activity.setText("Сохраняем промежуточные результаты…")
            if central := self.centralWidget():
                central.setEnabled(False)
            event.ignore()
        else:
            event.accept()


def main() -> int:
    smoke = "--smoke-test" in sys.argv
    app = QApplication([sys.argv[0]])
    app.setOrganizationName("UchebaAI")
    app.setApplicationName("UchebaAI")
    app.setApplicationDisplayName("Учёба AI")
    if smoke:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            data = Path(directory)
            settings = QSettings(str(data / "settings.ini"), QSettings.Format.IniFormat)
            window = MainWindow(data / "queue.sqlite3", settings)
            window.show()
            app.processEvents()
            window.close()
        return 0
    data = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "Учёба AI", "Приложение уже открыто.")
        return 1
    database = data / "queue.sqlite3"
    QueueStore(database).recover()
    window = MainWindow(database)
    window.show()
    return app.exec()
