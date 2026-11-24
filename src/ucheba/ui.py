"""Окно Windows для локальной и облачной обработки лекций."""

from __future__ import annotations

import sys
from bisect import bisect_right
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

from .analysis import OpenRouterError
from .documents import timestamp
from .imports import IMPORTS, MEDIA, load_transcript
from .models import Analysis, Segment
from .pipeline import partial_result, process
from .queue import Job, QueueStore
from .transcription import TranscriptionError
from .updates import Update, UpdateError, check_update, download_update

KEY_SERVICE = "UchebaAI"
KEY_ACCOUNT = "OpenRouter"
EXTENSIONS = MEDIA | IMPORTS
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
                    exc, (OpenRouterError, TranscriptionError)
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
    def __init__(self, parent: QWidget, update: Update | None, target: Path) -> None:
        super().__init__(parent)
        self.update = update
        self.target = target
        self.response: Update | Path | None = None
        self.error = ""

    def run(self) -> None:
        try:
            self.response = (
                check_update() if self.update is None else download_update(self.update, self.target)
            )
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
        self.installer: Path | None = None
        self.worker: UpdateThread | None = None
        self.setWindowTitle("Обновления")
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName("Что изменилось в новой версии")
        layout.addWidget(self.notes, 1)
        row = QHBoxLayout()
        self.release_button = QPushButton("Открыть выпуск")
        self.release_button.clicked.connect(self.open_release)
        row.addWidget(self.release_button)
        self.download_button = QPushButton("Загрузить установщик")
        self.download_button.clicked.connect(self.start_download)
        row.addWidget(self.download_button)
        self.install_button = QPushButton("Установить…")
        self.install_button.clicked.connect(self.start_install)
        row.addWidget(self.install_button)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.start_request(None)

    def start_request(self, update: Update | None) -> None:
        data = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        worker = UpdateThread(self, update, Path(data) / "Updates")
        self.worker = worker
        worker.finished.connect(self.request_finished)
        worker.finished.connect(worker.deleteLater)
        self.status.setText(
            "Проверка версии на GitHub…" if update is None else "Загрузка и проверка установщика…"
        )
        self.update_buttons()
        worker.start()

    def request_finished(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        result = worker.response
        if worker.error:
            self.status.setText(worker.error)
        elif isinstance(result, Update):
            self.available_update = result
            self.notes.setPlainText(result.notes)
            size = f"{result.installer_size / 1024**2:.1f}".replace(".", ",")
            self.status.setText(f"Доступна версия {result.version}. Размер установщика {size} МБ.")
        elif isinstance(result, Path):
            self.installer = result
            self.status.setText(f"Установщик проверен и сохранён в {result}")
        else:
            self.status.setText("Новых версий пока нет.")
        self.update_buttons()

    def update_buttons(self) -> None:
        idle = self.worker is None
        self.close_button.setEnabled(idle)
        self.release_button.setEnabled(self.available_update is not None)
        self.download_button.setEnabled(
            idle and self.available_update is not None and self.installer is None
        )
        self.install_button.setEnabled(idle and self.installer is not None and installed_copy())

    def open_release(self) -> None:
        if self.available_update:
            QDesktopServices.openUrl(QUrl(self.available_update.release_url))

    def start_download(self) -> None:
        if self.available_update and self.worker is None and self.installer is None:
            self.start_request(self.available_update)

    def start_install(self) -> None:
        if self.installer is None or not installed_copy():
            return
        if self.main_window.worker:
            self.status.setText("Дождитесь завершения текущей обработки перед установкой.")
            return
        was_paused = self.main_window.paused
        if not was_paused:
            self.main_window.toggle_pause()
        confirmed = QMessageBox.question(
            self,
            "Установка обновления",
            "Закрыть «Учёба AI» и запустить проверенный установщик?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed == QMessageBox.StandardButton.Yes:
            started, _pid = QProcess.startDetached(
                str(self.installer), [], str(self.installer.parent)
            )
            if started:
                self.accept()
                self.main_window.close()
                return
            self.status.setText("Не удалось запустить установщик.")
        if not was_paused:
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
                return Analysis.model_validate_json((result / "analysis.json").read_bytes())
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
        position = item.data(Qt.ItemDataRole.UserRole)
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
        cloud = QCheckBox("Отправить текст расшифровки в OpenRouter для анализа")
        form.addRow(cloud)
        free_note = QLabel(
            "Для бесплатного анализа используйте рабочую область OpenRouter без собственных ключей провайдеров."
        )
        free_note.setWordWrap(True)
        form.addRow(free_note)
        local = QLabel("Без облачного анализа приложение создаст полную расшифровку.")
        local.setWordWrap(True)
        form.addRow(local)
        if any(source.suffix.lower() in MEDIA for source in sources):
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
        unavailable = []
        for source in sources:
            try:
                self.queue.add(
                    source,
                    target,
                    model=models.currentText(),
                    device=device.currentData(),
                    cloud=cloud.isChecked(),
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
            player = LecturePlayer(job, self)
            player.exec()
            player.deleteLater()

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
